import numpy as np
import torch
from torch import nn
from functools import partial

from mlutils.training import LongCycler
from .main_loop_module import MainLoopModule


class NoiseAdvTraining(MainLoopModule):
    def __init__(self, model, config, device, data_loader, seed):
        super().__init__(model, config, device, data_loader, seed)
        self.progress = 0.0
        if isinstance(data_loader, LongCycler):
            data_loader = data_loader.loaders
        self.step_size = 1 / (config.max_iter * len(data_loader["img_classification"]))
        if config.noise_adv_regression:
            self.criterion = nn.MSELoss()
        else:  # config.noise_adv_classification
            self.criterion = nn.BCELoss()

    def pre_forward(self, model, inputs, shared_memory, train_mode, **kwargs):
        noise_adv_lambda = (
            2.0 / (1.0 + np.exp(-self.config.noise_adv_gamma * self.progress)) - 1
        )
        if train_mode:
            self.progress += self.step_size
        return partial(model, noise_lambda=noise_adv_lambda), inputs

    def post_forward(
        self,
        outputs,
        loss,
        targets,
        extra_losses,
        train_mode,
        applied_std=None,
        **kwargs
    ):
        extra_outputs = outputs[0]
        if applied_std is None:
            applied_std = torch.zeros_like(
                extra_outputs["noise_pred"], device=self.device
            )
        if self.config.noise_adv_classification:
            applied_std = (
                (applied_std > 0.0).type(torch.FloatTensor).to(device=self.device)
            )
        noise_loss = self.criterion(extra_outputs["noise_pred"], applied_std)
        extra_losses["NoiseAdvTraining"] += noise_loss.item()
        loss += self.config.noise_adv_loss_factor * noise_loss
        return outputs, loss, targets
