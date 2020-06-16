import collections

import numpy as np
import torch
import copy

from torch import optim

from bias_transfer.models.utils import weight_reset
from .main_loop_module import MainLoopModule

EPS = 1e-6


class LotteryTicketPruning(MainLoopModule):
    """
    Based on the implementation from https://github.com/rahulvigneswaran/Lottery-Ticket-Hypothesis-in-Pytorch
    (therefore indirectly from https://github.com/ktkth5/lottery-ticket-hyopothesis)
    """

    def __init__(self, model, config, device, data_loader, seed):
        super().__init__(model, config, device, data_loader, seed)
        if self.config.lottery_ticket.get("pruning", True):
            n_epochs = self.config.max_iter
            n_rounds = self.config.lottery_ticket.get("rounds", 1)
            percent_to_prune = self.config.lottery_ticket.get("percent_to_prune", 80)
            self.percent_per_round = (
                1 - (1 - percent_to_prune / 100) ** (1 / n_rounds)
            ) * 100
            self.reset_epochs = [
                r * self.config.lottery_ticket.get("round_length", 100) + 1
                for r in range(1, n_rounds + 1)
            ]
            print("Percent to prune per round:", self.percent_per_round, flush=True)
            print("Reset before epochs:", list(self.reset_epochs), flush=True)

            # create initial (empty mask):
            self.mask = self.make_empty_mask(model)

            # save initial state_dict to reset to this point later:
            if not self.config.lottery_ticket.get("reinit"):
                self.initial_state_dict = copy.deepcopy(model.state_dict())
            self.initial_optim_state_dict = None
            self.initial_scheduler_state_dict = None
            self.initial_a_scheduler_state_dict = None

    def pre_epoch(
        self, model, train_mode, epoch, optimizer=None, lr_scheduler=None, **kwargs
    ):
        if not self.initial_optim_state_dict:
            self.initial_optim_state_dict = copy.deepcopy(optimizer.state_dict())
        if not self.initial_scheduler_state_dict:
            self.initial_scheduler_state_dict = copy.deepcopy(lr_scheduler.state_dict())
            if hasattr(lr_scheduler, "after_scheduler") and lr_scheduler.after_scheduler:  # for warmup
                self.initial_a_scheduler_state_dict = copy.deepcopy(lr_scheduler.after_scheduler.state_dict())
        if (
            self.config.lottery_ticket.get("pruning", True)
            and epoch in self.reset_epochs
            and epoch > 0  # validation calls this with epoch = 0
        ):
            # Prune the network, i.e. update the mask
            self.prune_by_percentile(model, self.percent_per_round)
            print("Reset init in Epoch ", epoch, flush=True)
            self.reset_initialization(model, self.config.lottery_ticket.get("reinit"))
            # Reset lr and scheduler:
            if hasattr(lr_scheduler, "after_scheduler") and lr_scheduler.after_scheduler:  # for warmup
                lr_scheduler.finished = False
                lr_scheduler.after_scheduler.load_state_dict(copy.deepcopy(self.initial_a_scheduler_state_dict))
                lr_scheduler.after_scheduler._step_count = 0
                lr_scheduler.after_scheduler.last_epoch = 0
                lr_scheduler.after_scheduler._get_lr_called_within_step = True
            optimizer.load_state_dict(copy.deepcopy(self.initial_optim_state_dict))
            lr_scheduler.load_state_dict(copy.deepcopy(self.initial_scheduler_state_dict))
            lr_scheduler._step_count = 0
            optimizer._step_count = 0
            lr_scheduler.last_epoch = 0

    def post_backward(self, model, **kwargs):
        # Freezing Pruned weights by making their gradients Zero
        for name, p in model.named_parameters():
            if "weight" in name and self.config.readout_name not in name:
                tensor = torch.abs(p.data)
                grad_tensor = p.grad.data
                p.grad.data = torch.where(
                    tensor < EPS, torch.zeros_like(grad_tensor), grad_tensor
                )

    def prune_by_percentile(self, model, percent):
        # Calculate percentile value
        if self.config.lottery_ticket.get("global_pruning"):
            alive_tensors = []
            step = 0
            for name, param in model.named_parameters():
                if (
                    "weight" in name and self.config.readout_name not in name
                ):  # We do not prune bias term
                    alive_tensors.append(
                        param.data[torch.nonzero(self.mask[step], as_tuple=True)]
                    )  # flattened array of nonzero values
                    step += 1
            alive = torch.cat(alive_tensors)
            percentile_value = np.percentile(torch.abs(alive).cpu().numpy(), percent)

        step = 0
        for name, param in model.named_parameters():
            if (
                "weight" in name and self.config.readout_name not in name
            ):  # We do not prune bias term
                if not self.config.lottery_ticket.get("global_pruning"):
                    # print(nonzero)
                    alive = param.data[
                        torch.nonzero(self.mask[step], as_tuple=True)
                    ]  # flattened array of nonzero values
                    abs_alive = torch.abs(alive).cpu().numpy()
                    percentile_value = np.percentile(abs_alive, percent)

                # Convert Tensors to numpy and calculate
                new_mask = torch.where(
                    torch.abs(param.data)
                    < torch.tensor(percentile_value, device=param.data.device),
                    torch.zeros_like(self.mask[step]),
                    self.mask[step],
                )

                # Apply new weight and mask
                param.data = param.data * new_mask
                self.mask[step] = new_mask
                step += 1

    def make_empty_mask(self, model):
        """
        Function to make an empty mask of the same size as the model
        :param model:
        :return: mask
        """
        step = 0
        for name, param in model.named_parameters():
            if "weight" in name and self.config.readout_name not in name:
                step = step + 1
        mask = [None] * step
        step = 0
        for name, param in model.named_parameters():
            if "weight" in name and self.config.readout_name not in name:
                tensor = param.data
                mask[step] = torch.ones_like(tensor, device=tensor.device)
                step = step + 1
        return mask

    def reset_initialization(self, model, reinit=False):
        if reinit:
            model.apply(weight_reset)  # new random init
        step = 0
        for name, param in model.named_parameters():
            init = param.data if reinit else self.initial_state_dict[name]
            if "weight" in name and self.config.readout_name not in name:
                param.data = self.mask[step] * init
                step = step + 1
            elif "bias" in name or "weight" in name:
                param.data = init
