import torch
from torch import nn
from torch.utils.data import TensorDataset

from bias_transfer.configs.trainer import TrainerConfig
from bias_transfer.dataset.combined_dataset import CombinedDataset, JoinedDataset
from bias_transfer.models.utils import weight_reset, freeze_params
from bias_transfer.trainer.main_loop_modules import ModelWrapper
from bias_transfer.utils.io import restore_saved_state
from mlutils.training import LongCycler


def compute_representation(model, criterion, device, data_loader, rep_name):
    task_dict, module_losses, collected_outputs = main_loop(
        model=model,
        criterion=criterion,
        device=device,
        optimizer=None,
        data_loader=data_loader,
        epoch=0,
        n_iterations=len(data_loader),
        modules=[
            ModelWrapper(None, TrainerConfig(comment=""), None, None, None)
        ],  # The data is already modified to have
        train_mode=False,
        return_outputs=True,
    )
    outputs = [o[rep_name] for o in collected_outputs]
    return torch.cat(outputs)


def generate_rep_dataset(model, criterion, device, data_loader, rep_name):
    data_loader = data_loader["img_classification"]
    data_loader_ = torch.utils.data.DataLoader(
        data_loader.dataset,
        batch_size=data_loader.batch_size,
        sampler=None,  # make sure the dataset is in the right order and complete
        num_workers=data_loader.num_workers,
        pin_memory=data_loader.pin_memory,
        shuffle=False,
    )
    representation = compute_representation(
        model, criterion, device, {"img_classification": data_loader_}, rep_name
    )
    rep_dataset = TensorDataset(representation.to("cpu"))
    img_dataset = data_loader.dataset
    combined_dataset = CombinedDataset(
        JoinedDataset(
            sample_datasets=[img_dataset], target_datasets=[img_dataset, rep_dataset]
        )
    )
    combined_data_loader = torch.utils.data.DataLoader(
        dataset=combined_dataset,
        batch_size=data_loader.batch_size,
        sampler=data_loader.sampler,
        num_workers=data_loader.num_workers,
        pin_memory=data_loader.pin_memory,
        shuffle=False,
    )
    return {"img_classification": combined_data_loader}


def transfer_model(to_model, config, criterion=None, device=None, data_loader=None, restriction=None):
    model = restore_saved_state(
        to_model,
        config.transfer_from_path,
        ignore_missing=True,
        ignore_dim_mismatch=True,
        ignore_unused=True,
        match_names=True,
        restriction=restriction
    )
    if config.rdm_transfer:
        data_loader = generate_rep_dataset(
            model, criterion, device, data_loader, "core"
        )
        model.apply(
            weight_reset
        )  # model was only used to generated representations now we clear it again
    elif config.reset_linear:
        print("Readout is being reset")
        if isinstance(model, nn.DataParallel):
            model = model.module
        getattr(model, config.readout_name).apply(weight_reset)
    return data_loader
