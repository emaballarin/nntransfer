from torch import nn
import torch

from .main_loop_module import MainLoopModule


def compute_corr_matrix(x):
    x_flat = x.flatten(1, -1)
    centered = x_flat - x_flat.mean(dim=1).view(-1, 1)
    result = (centered @ centered.transpose(0, 1)) / torch.ger(
        torch.norm(centered, 2, dim=1), torch.norm(centered, 2, dim=1)
    )  # see https://de.mathworks.com/help/images/ref/corr2.html
    return result


def compute_cosine_matrix(x):
    x_flat = x.flatten(1, -1)
    centered = x_flat - x_flat.mean(dim=0).view(1, -1)  # centered by mean over images
    result = (centered @ centered.transpose(0, 1)) / torch.ger(
        torch.norm(centered, 2, dim=1), torch.norm(centered, 2, dim=1)
    )  # see https://de.mathworks.com/help/images/ref/corr2.html
    return result


def arctanh(x):
    return 0.5 * torch.log((1 + x) / (1 - x))


class RDMPrediction(MainLoopModule):
    def __init__(self, trainer):
        super().__init__(trainer)
        self.criterion = nn.MSELoss()

    def post_forward(self, outputs, loss, targets, **shared_memory):
        extra_outputs = outputs[0]
        if self.train_mode:
            pred_rdm = compute_cosine_matrix(extra_outputs["core"])
            pred_rdm = arctanh(pred_rdm.triu(diagonal=1))
            trg_rdm = compute_cosine_matrix(targets[1])
            trg_rdm = arctanh(trg_rdm.triu(diagonal=1))
            pred_loss = self.criterion(pred_rdm, trg_rdm)
            loss += self.config.rdm_prediction.get("lambda", 1.0) * pred_loss
            self.tracker.log_objective(
                pred_loss.item(), (self.mode, self.task_key, "RDMPrediction")
            )
            return outputs, loss, targets[0]
        else:
            return outputs, loss, targets
