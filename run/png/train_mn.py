import argparse
import math
import os
import random
import sys
import time

from tabulate import tabulate
from PIL import Image
from pathlib import Path

import chainer
import chainermn
import chainer.functions as cf
import cupy
import numpy as np
from chainer.backends import cuda

sys.path.append(".")
sys.path.append(os.path.join("..", ".."))
import glow

from hyperparams import Hyperparameters
from model import InferenceModel, GenerativeModel
from optimizer import Optimizer


def to_gpu(array):
    if isinstance(array, np.ndarray):
        return cuda.to_gpu(array)
    return array


def to_cpu(array):
    if isinstance(array, cupy.ndarray):
        return cuda.to_cpu(array)
    return array


def printr(string):
    sys.stdout.write(string)
    sys.stdout.write("\r")


# return z of same shape as x
def merge_factorized_z(factorized_z, factor=2):
    z = None
    for zi in reversed(factorized_z):
        xp = cuda.get_array_module(zi.data)
        z = zi.data if z is None else xp.concatenate((zi.data, z), axis=1)
        z = glow.nn.chainer.functions.unsqueeze(z, factor, xp)
    return z


def main():
    try:
        os.mkdir(args.snapshot_path)
    except:
        pass

    comm = chainermn.create_communicator()
    device = comm.intra_rank
    print("device", device, "/", comm.size)
    cuda.get_device(device).use()
    xp = cupy

    num_bins_x = 2**args.num_bits_x

    images = None
    if comm.rank == 0:
        files = Path(args.dataset_path).glob("*.png")
        images = []
        for filepath in files:
            image = np.array(Image.open(filepath)).astype("float32")
            if args.num_bits_x < 8:
                image = np.floor(image / (2**(8 - args.num_bits_x)))
            image = image / num_bins_x - 0.5
            image = image.transpose((2, 0, 1))
            images.append(image)
        images = np.asanyarray(images)

        x_mean = np.mean(images)
        x_var = np.var(images)

        print(
            tabulate([
                ["#", len(images)],
                ["mean", x_mean],
                ["var", x_var],
            ]))

    dataset = chainermn.scatter_dataset(images, comm, shuffle=True)
    iterator = chainer.iterators.SerialIterator(dataset, args.batch_size)

    hyperparams = Hyperparameters(args.snapshot_path)
    hyperparams.levels = args.levels
    hyperparams.depth_per_level = args.depth_per_level
    hyperparams.nn_hidden_channels = args.nn_hidden_channels
    hyperparams.image_size = image.shape[1:]
    hyperparams.num_bits_x = args.num_bits_x
    hyperparams.lu_decomposition = args.lu_decomposition
    hyperparams.serialize(args.snapshot_path)

    print(
        tabulate([
            ["levels", hyperparams.levels],
            ["depth_per_level", hyperparams.depth_per_level],
            ["nn_hidden_channels", hyperparams.nn_hidden_channels],
            ["image_size", hyperparams.image_size],
            ["lu_decomposition", hyperparams.lu_decomposition],
            ["num_bits_x", hyperparams.num_bits_x],
        ]))

    encoder = InferenceModel(hyperparams, hdf5_path=args.snapshot_path)
    encoder.to_gpu()

    optimizer = chainermn.create_multi_node_optimizer(
        chainer.optimizers.Adam(alpha=1.0 * 1e-4), comm)
    optimizer.setup(encoder.parameters)

    # Data dependent initialization
    if encoder.need_initialize:
        for data in iterator:
            x = to_gpu(np.asanyarray(data))
            encoder.initialize_actnorm_weights(x)
            break

    current_training_step = 0
    num_pixels = hyperparams.image_size[0] * hyperparams.image_size[1]

    # Training loop
    for iteration in range(args.training_steps):
        sum_loss = 0
        start_time = time.time()

        for batch_index, data in enumerate(iterator):
            x = to_gpu(np.asanyarray(data))
            x += xp.random.uniform(0, 1.0 / num_bins_x, size=x.shape)
            factorized_z, logdet = encoder(x, reduce_memory=args.reduce_memory)
            logdet -= math.log(num_bins_x) * num_pixels
            negative_log_likelihood = 0
            for zi in factorized_z:
                negative_log_likelihood += glow.nn.chainer.functions.standard_normal_nll(
                    zi)
            denom = math.log(2.0) * args.batch_size * num_pixels
            loss = (negative_log_likelihood - logdet) / denom
            encoder.cleargrads()
            loss.backward()
            optimizer.update()

            current_training_step += 1

            sum_loss += float(loss.data)
            printr(
                "Iteration {}: Batch {} / {} - loss: {:.8f} - nll: {:.8f} - log_det: {:.8f}".
                format(iteration + 1, batch_index + 1, len(dataset),
                       float(loss.data),
                       float(negative_log_likelihood.data) / denom,
                       float(logdet.data) / denom))

            if batch_index % 100 == 0:
                encoder.serialize(args.snapshot_path)

        # Check model reversibility
        rev_x_mean = None
        rev_x_var = None
        z_mean = None
        z_var = None
        if True:
            with chainer.no_backprop_mode():
                decoder = encoder.reverse()
                decoder.to_gpu()
                factorized_z, logdet = encoder(x)
                rev_x = decoder(factorized_z)
                rev_x_mean = float(xp.mean(rev_x.data))
                rev_x_var = float(xp.var(rev_x.data))

                z = merge_factorized_z(factorized_z)
                z_mean = float(xp.mean(z))
                z_var = float(xp.var(z))

        elapsed_time = time.time() - start_time
        print(
            "\033[2KIteration {} - loss: {:.5f} - z: mean={:.5f} var={:.5f} - rev_x: mean={:.5f} var={:.5f} - elapsed_time: {:.3f} min".
            format(iteration + 1, sum_loss / len(iterator), z_mean, z_var,
                   rev_x_mean, rev_x_var, elapsed_time / 60))
        encoder.serialize(args.snapshot_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-path", "-dataset", type=str, required=True)
    parser.add_argument(
        "--snapshot-path", "-snapshot", type=str, default="snapshot")
    parser.add_argument("--batch-size", "-b", type=int, default=32)
    parser.add_argument("--reduce-memory", action="store_true")
    parser.add_argument("--training-steps", "-i", type=int, default=100000)
    parser.add_argument("--depth-per-level", "-depth", type=int, default=32)
    parser.add_argument("--levels", "-levels", type=int, default=5)
    parser.add_argument("--nn-hidden-channels", "-nn", type=int, default=512)
    parser.add_argument("--num-bits-x", "-bits", type=int, default=8)
    parser.add_argument("--lu-decomposition", "-lu", action="store_true")
    args = parser.parse_args()
    main()
