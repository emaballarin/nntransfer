import copy
import os
import numpy as np
import torch
import torchvision
import torchvision.transforms as transforms
from torch.utils.data.dataset import ConcatDataset, Subset
from torch.utils.data.sampler import SubsetRandomSampler
from torchvision import datasets
from nntransfer.configs.dataset.image import ImageDatasetConfig
from nntransfer.dataset.utils import get_dataset
from .dataset_classes.npy_dataset import NpyDataset
from .dataset_filters import *

DATASET_URLS = {
    "CIFAR10-C": "https://zenodo.org/record/2535967/files/CIFAR-10-C.tar",
    "CIFAR100-C": "https://zenodo.org/record/3555552/files/CIFAR-100-C.tar",
    "MNIST-C": "https://zenodo.org/record/3239543/files/mnist_c.zip",
    "TinyImageNet-C": "https://zenodo.org/record/2536630/files/Tiny-ImageNet-C.tar",
    "TinyImageNet-ST": "https://informatikunihamburgde-my.sharepoint.com/:u:/g/personal/shahd_safarani_informatik_uni-hamburg_de/EZhUKKVXTvRHlqi2HXHaIjEBLmAv4tQP8olvdGNRoWrPqA?e=8kSrHI&download=1",
    "ImageNet-C": {
        "blur": "https://zenodo.org/record/2235448/files/blur.tar",
        "digital": "https://zenodo.org/record/2235448/files/digital.tar",
        "extra": "https://zenodo.org/record/2235448/files/extra.tar",
        "noise": "https://zenodo.org/record/2235448/files/noise.tar",
        "weather": "https://zenodo.org/record/2235448/files/weather.tar",
    },
}

import torchvision.transforms.functional as tF
import random
from typing import Sequence


class DiscreteRotateTransform:
    def __init__(self, angles: Sequence[int]):
        self.angles = angles

    def __call__(self, x):
        angle = random.choice(self.angles)
        return tF.rotate(x, angle)


class ImageDatasetLoader:
    def __call__(self, seed, **config):
        """
        Utility function for loading and returning train and valid
        multi-process iterators over the CIFAR-10 dataset. A sample
        9x9 grid of the images can be optionally displayed.
        If using CUDA, num_workers should be set to 1 and pin_memory to True.
        Params
        ------
        - data_dir: path directory to the dataset.
        - batch_size: how many samples per batch to load.
        - augment: whether to apply the data augmentation scheme
          mentioned in the paper. Only applied on the train split.
        - seed: fix seed for reproducibility.
        - valid_size: percentage split of the training set used for
          the validation set. Should be a float in the range [0, 1].
        - shuffle: whether to shuffle the train/validation indices.
        - show_sample: plot 9x9 sample grid of the dataset.
        - num_workers: number of subprocesses to use when loading the dataset.
        - pin_memory: whether to copy tensors into CUDA pinned memory. Set it to
          True if using GPU.
        Returns
        -------
        - train_loader: training set iterator.
        - valid_loader: validation set iterator.
        """
        config = ImageDatasetConfig.from_dict(config)
        print("Loading dataset: {}".format(config.dataset_cls))
        torch.manual_seed(seed)
        np.random.seed(seed)

        transform_test, transform_train, transform_val = self.get_transforms(config)

        error_msg = "[!] valid_size should be in the range [0, 1]."
        assert (config.valid_size >= 0) and (config.valid_size <= 1), error_msg

        (
            train_dataset,
            valid_dataset,
            test_dataset,
            c_test_datasets,
            st_test_dataset,
            rotated_test_dataset,
        ) = self.get_datasets(config, transform_test, transform_train, transform_val)

        filters = [globals().get(f)(config, train_dataset) for f in config.filters]
        datasets_ = [train_dataset, valid_dataset, test_dataset]
        if config.add_corrupted_test:
            for c_ds in c_test_datasets.values():
                datasets_ += list(c_ds.values())
        for ds in datasets_:
            for filt in filters:
                filt.apply(ds)

        data_loaders = self.get_data_loaders(
            st_test_dataset,
            c_test_datasets,
            rotated_test_dataset,
            config,
            seed,
            test_dataset,
            train_dataset,
            valid_dataset,
        )

        return data_loaders

    def get_transforms(self, config):
        """

        Args:
            config:

        Returns:
            transform_test,
            transform_train,
            transform_val
        """
        raise NotImplementedError()

    def get_datasets(self, config, transform_test, transform_train, transform_val):
        """

        Args:
            config:
            transform_test:
            transform_train:
            transform_val:

        Returns:
            train_dataset,
            valid_dataset,
            test_dataset,
            c_test_datasets,
            st_test_dataset,
        """
        raise NotImplementedError()

    def add_corrupted_test(self, config, transform_test):
        c_test_datasets = None
        if config.add_corrupted_test:
            c_class =config.dataset_cls + "-C" if "-C" not in config.dataset_cls else config.dataset_cls
            urls = DATASET_URLS[c_class]
            if not isinstance(urls, dict):
                urls = {"default": urls}
            for key, url in urls.items():
                dataset_dir = get_dataset(
                    url,
                    config.data_dir,
                    dataset_cls=c_class,
                )

                c_test_datasets = {}
                for c_category in os.listdir(dataset_dir):
                    if config.dataset_cls in ("CIFAR10", "CIFAR100"):
                        if c_category == "labels.npy" or not c_category.endswith(
                            ".npy"
                        ):
                            continue
                        c_test_datasets[c_category[:-4]] = {}
                        for c_level in range(1, 6):
                            start = (c_level - 1) * 10000
                            end = c_level * 10000
                            c_test_datasets[c_category[:-4]][c_level] = NpyDataset(
                                samples=c_category,
                                targets="labels.npy",
                                root=dataset_dir,
                                start=start,
                                end=end,
                                transform=transform_test,
                            )
                    if config.dataset_cls in ("MNIST", "MNIST-C"):
                        if "finished" in c_category or c_category != "translate":
                            continue
                        c_test_datasets[c_category] = {
                            1: NpyDataset(
                                samples="test_images.npy",
                                targets="test_labels.npy",
                                root=os.path.join(dataset_dir, c_category),
                                transform=transform_test,
                                expect_channel_last=True,
                                samples_as_torch=False,
                            )
                        }
                    else:
                        if not os.path.isdir(os.path.join(dataset_dir, c_category)):
                            continue
                        c_test_datasets[c_category] = {}
                        for c_level in os.listdir(
                            os.path.join(dataset_dir, c_category)
                        ):
                            c_test_datasets[c_category][
                                int(c_level)
                            ] = datasets.ImageFolder(
                                os.path.join(dataset_dir, c_category, c_level),
                                transform=transform_test,
                            )
        return c_test_datasets

    def add_rotated_test(self, config, test_dataset):
        rotated_test_dataset = None
        if config.add_rotated_test:
            rotated_test_dataset = copy.deepcopy(test_dataset)
            rotated_test_dataset.transform = transforms.Compose(
                [DiscreteRotateTransform([0, 90, 180, 270]), test_dataset.transform]
            )
        return rotated_test_dataset

    def add_stylized_test(self, config, transform_test):
        st_test_dataset = None
        if config.add_stylized_test:
            st_dataset_dir = get_dataset(
                DATASET_URLS[config.dataset_cls + "-ST"],
                config.data_dir,
                dataset_cls=config.dataset_cls + "-ST",
            )
            st_test_dataset = datasets.ImageFolder(
                st_dataset_dir, transform=transform_test
            )
        return st_test_dataset

    def get_data_loaders(
        self,
        st_test_dataset,
        c_test_datasets,
        rot_test_dataset,
        config,
        seed,
        test_dataset,
        train_dataset,
        valid_dataset,
    ):
        num_train = len(train_dataset)
        indices = list(range(num_train))
        if config.use_c_test_as_val:  # Use valid_size of the c_test set for validation
            train_sampler = SubsetRandomSampler(indices)
            datasets = []
            val_indices = []
            start_idx = 0
            for c_category in c_test_datasets.keys():
                if c_category not in (
                    "speckle_noise",
                    "gaussian_blur",
                    "spatter",
                    "saturate",
                ):
                    continue
                for dataset in c_test_datasets[c_category].values():
                    num_val = len(dataset)
                    indices = list(range(start_idx, start_idx + num_val))
                    split = int(np.floor(config.valid_size * num_val))
                    if config.shuffle:
                        np.random.shuffle(indices)
                    val_indices += indices[:split]
                    datasets.append(dataset)
                    start_idx += num_val
            valid_dataset = ConcatDataset(datasets)
            valid_sampler = SubsetRandomSampler(val_indices)
        else:  # Use valid_size of the train set for validation
            split = int(np.floor(config.valid_size * num_train))
            if config.shuffle:
                np.random.seed(seed)
                np.random.shuffle(indices)
            train_idx, valid_idx = indices[split:], indices[:split]
            if config.train_subset:
                subset_split = int(np.floor(config.train_subset * len(train_idx)))
                train_idx = train_idx[:subset_split]
            if config.shuffle:
                train_sampler = SubsetRandomSampler(train_idx)
                valid_sampler = SubsetRandomSampler(valid_idx)
            else:
                train_dataset = Subset(train_dataset, train_idx)
                valid_dataset = Subset(train_dataset, valid_idx)
                train_sampler = None
                valid_sampler = None
        train_loader = torch.utils.data.DataLoader(
            train_dataset,
            batch_size=config.batch_size,
            sampler=train_sampler,
            num_workers=config.num_workers,
            pin_memory=config.pin_memory,
            shuffle=False,
        )
        valid_loader = torch.utils.data.DataLoader(
            valid_dataset,
            batch_size=config.batch_size,
            sampler=valid_sampler,
            num_workers=config.num_workers,
            pin_memory=config.pin_memory,
            shuffle=False,
        )
        test_loader = torch.utils.data.DataLoader(
            test_dataset,
            batch_size=config.batch_size,
            num_workers=config.num_workers,
            pin_memory=config.pin_memory,
            shuffle=True,
        )
        task_key = (
            "regression"
            if config.bias is not None and "regression" in config.bias
            else "img_classification"
        )
        data_loaders = {
            "train": {task_key: train_loader},
            "validation": {task_key: valid_loader},
            "test": {task_key: test_loader},
        }

        if config.add_stylized_test:
            st_test_loader = torch.utils.data.DataLoader(
                st_test_dataset,
                batch_size=config.batch_size,
                num_workers=config.num_workers,
                pin_memory=config.pin_memory,
                shuffle=False,
            )
            data_loaders["st_test"] = st_test_loader

        if config.add_corrupted_test:
            c_test_loaders = {}
            for c_category in c_test_datasets.keys():
                c_test_loaders[c_category] = {}
                for c_level, dataset in c_test_datasets[c_category].items():
                    c_test_loaders[c_category][c_level] = torch.utils.data.DataLoader(
                        dataset,
                        batch_size=config.batch_size,
                        num_workers=config.num_workers,
                        pin_memory=config.pin_memory,
                        shuffle=True,
                    )
            data_loaders["c_test"] = {"img_classification": c_test_loaders}
        if config.add_rotated_test:
            data_loaders["rot_test"] = torch.utils.data.DataLoader(
                rot_test_dataset,
                batch_size=config.batch_size,
                num_workers=config.num_workers,
                pin_memory=config.pin_memory,
                shuffle=True,
            )
        return data_loaders
