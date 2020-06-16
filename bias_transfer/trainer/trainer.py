import os
from functools import partial

import numpy as np
import torch
from torch import nn, optim
from torch.backends import cudnn as cudnn

from bias_transfer.models.utils import freeze_params
from bias_transfer.utils.warmup import GradualWarmupScheduler
from bias_transfer.trainer.utils import (
    get_subdict,
    StopClosureWrapper,
)
from mlutils.training import LongCycler
import nnfabrik as nnf
from bias_transfer.trainer.main_loop_modules import *
from bias_transfer.configs.trainer import TrainerConfig
from bias_transfer.trainer import main_loop
from bias_transfer.trainer.transfer import transfer_model
from bias_transfer.trainer.main_loop import main_loop
from bias_transfer.trainer.test import test_neural_model, test_model
from bias_transfer.utils.io import load_model, load_checkpoint, save_checkpoint
from mlutils import measures as mlmeasures
from mlutils.training import MultipleObjectiveTracker #, early_stopping
from .utils import early_stopping
from nnvision.utility import measures
from nnvision.utility.measures import get_correlations, get_poisson_loss
from .utils import save_best_model, XEntropyLossWrapper, NBLossWrapper
from bias_transfer.trainer import utils as uts


def trainer(model, dataloaders, seed, uid, cb, eval_only=False, **kwargs):
    config = TrainerConfig.from_dict(kwargs)
    uid = nnf.utility.dj_helpers.make_hash(uid)
    device = "cuda" if torch.cuda.is_available() and not config.force_cpu else "cpu"
    best_epoch = 0
    torch.manual_seed(seed)
    np.random.seed(seed)

    # Model
    print("==> Building model..", flush=True)
    model = model.to(device)
    if device == "cuda":
        cudnn.benchmark = False
        cudnn.deterministic = True
        torch.cuda.manual_seed(seed)

    if config.mtl and ("neural" not in config.loss_functions.keys()):
        dataloaders["train"] = get_subdict(dataloaders["train"], ["img_classification"])
        dataloaders["validation"] = get_subdict(dataloaders["validation"], ["img_classification"])
        dataloaders["test"] = get_subdict(dataloaders["test"], ["img_classification"])

    train_loader = getattr(uts, config.train_cycler)(dataloaders["train"], **config.train_cycler_args)

    train_n_iterations = len(train_loader)
    optim_step_count = (
        len(dataloaders["train"].keys())
        if config.loss_accum_batch_n is None
        else config.loss_accum_batch_n
    )

    val_keys = dataloaders["validation"].keys()
    val_n_iterations = {
        k: len(LongCycler(dataset)) if "img_classification" not in k else len(dataset)
        for k, dataset in dataloaders["validation"].items()
    }
    test_n_iterations = {
        k: None if "img_classification" not in k else len(dataset)
        for k, dataset in dataloaders["test"].items()
    }
    best_eval = {k: {"eval": -100000, "loss": 100000} for k in val_keys}
    # Main-loop modules:
    main_loop_modules = [
        globals().get(k)(model, config, device, train_loader, seed)
        for k in config.main_loop_modules
    ]

    criterion, stop_closure = {}, {}
    for k in val_keys:
        if "img_classification" not in k:
            if config.loss_weighing:
                criterion[k] = NBLossWrapper().to(device)
            else:
                criterion[k] = getattr(mlmeasures, config.loss_functions[k])(
                    avg=config.avg_loss
                )
            stop_closure[k] = {}
            stop_closure[k]['eval'] = partial(
                getattr(measures, "get_correlations"),
                dataloaders=dataloaders["validation"][k],
                device=device,
                per_neuron=False,
                avg=True,
            )
            stop_closure[k]['loss'] = partial(
                get_poisson_loss,
                dataloaders=dataloaders["validation"][k],
                device=device,
                per_neuron=False,
                avg=False,
            )
        else:
            if config.loss_weighing:
                criterion[k] = XEntropyLossWrapper(
                    getattr(nn, config.loss_functions[k])()
                ).to(device)
            else:
                criterion[k] = getattr(nn, config.loss_functions[k])()
            stop_closure[k] = partial(
                main_loop,
                criterion=get_subdict(criterion, [k]),
                device=device,
                data_loader=get_subdict(dataloaders["validation"], [k]),
                modules=main_loop_modules,
                train_mode=False,
                n_iterations=val_n_iterations[k],
                return_outputs=False,
                scale_loss=config.scale_loss,
                optim_step_count=optim_step_count,
                eval_type="Validation",
                epoch=0,
                optimizer=None,
                loss_weighing=config.loss_weighing,
                cycler_args={},
                cycler="LongCycler"
            )

    if config.track_training:
        tracker_dict = dict(
            correlation=partial(
                get_correlations(),
                model,
                dataloaders["validation"],
                device=device,
                per_neuron=False,
            ),
            poisson_loss=partial(
                get_poisson_loss(),
                model,
                dataloaders["validation"],
                device=device,
                per_neuron=False,
                avg=False,
            ),
        )
        if hasattr(model, "tracked_values"):
            tracker_dict.update(model.tracked_values)
        tracker = MultipleObjectiveTracker(**tracker_dict)
    else:
        tracker = None

    params = list(model.parameters())
    if config.loss_weighing:
        for _, loss_object in criterion.items():
            params += list(loss_object.parameters())
    optimizer = getattr(optim, config.optimizer)(params, **config.optimizer_options)
    if config.scheduler is not None:
        if config.scheduler == "adaptive":
            if config.scheduler_options['mtl']:
                lr_scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=config.lr_decay)
            elif not config.scheduler_options['mtl']:
                lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(
                    optimizer,
                    factor=config.lr_decay,
                    patience=config.patience,
                    threshold=config.threshold,
                    verbose=config.verbose,
                    min_lr=config.min_lr,
                    mode="max" if config.maximize else "min",
                    threshold_mode=config.threshold_mode,
                )
        elif config.scheduler == "manual":
            lr_scheduler = optim.lr_scheduler.MultiStepLR(
                optimizer, milestones=config.scheduler_options['milestones'], gamma=config.lr_decay
            )
    else:
        lr_scheduler = None

    if config.lr_warmup:
        if lr_scheduler is None:
            raise Exception("Warmup scheduler needs normal lr-scheduler to work!")
        lr_scheduler = GradualWarmupScheduler(
            optimizer,
            multiplier=1,  # starts from 0
            total_epoch=config.lr_warmup,  # gradually raises lr over x epochs
            after_scheduler=lr_scheduler,  # this scheduler will be applied after warmup
        )

    start_epoch = config.epoch
    path = "./checkpoint/ckpt.{}.pth".format(uid)
    if os.path.isfile(path):
        model, best_eval, start_epoch = load_checkpoint(path, model, optimizer)
        best_epoch = start_epoch
    elif config.transfer_from_path:
        dataloaders["train"] = transfer_model(
            model,
            config,
            criterion=criterion,
            device=device,
            data_loader=dataloaders["train"],
        )

    if config.freeze:
        if config.freeze == ("core",):
            kwargs = {"not_to_freeze": (config.readout_name,)}
        elif config.freeze == ("readout",):
            kwargs = {"to_freeze": (config.readout_name,)}
        else:
            kwargs = {"to_freeze": config.freeze}
        freeze_params(model, **kwargs)

    print("==> Starting model {}".format(config.comment), flush=True)
    train_stats = []
    epoch_iterator = early_stopping(
        model,
        stop_closure,
        config,
        interval=config.interval,
        patience=config.patience,
        start=start_epoch,
        max_iter=config.max_iter,
        maximize=config.maximize,
        tolerance=config.threshold,
        restore_best=config.restore_best,
        tracker=tracker,
        scheduler=lr_scheduler,
        lr_decay_steps=config.lr_decay_steps,
    )

    # train over epochs
    train_results, train_module_loss, epoch = 0, 0, 0
    for epoch, dev_eval in epoch_iterator:
        if cb:
            cb()

        if config.verbose and tracker is not None:
            print("=======================================")
            for key in tracker.log.keys():
                print(key, tracker.log[key][-1], flush=True)

        if epoch > 1:
            best_epoch, best_eval = save_best_model(
                model, optimizer, dev_eval, epoch, best_eval, best_epoch, uid
            )

            train_stats.append(
                {
                    "train_results": train_results,
                    "train_module_loss": train_module_loss,
                    "dev_eval": dev_eval,
                }
            )

        train_results, train_module_loss = main_loop(
            model=model,
            criterion=criterion,
            device=device,
            optimizer=optimizer,
            data_loader=dataloaders["train"],
            n_iterations=train_n_iterations,
            modules=main_loop_modules,
            train_mode=True,
            epoch=epoch,
            optim_step_count=optim_step_count,
            cycler=config.train_cycler,
            cycler_args=config.train_cycler_args,
            loss_weighing=config.loss_weighing,
            scale_loss=config.scale_loss,
            lr_scheduler=lr_scheduler
        )

    dev_eval = StopClosureWrapper(stop_closure)(model)
    if epoch > 0:
        best_epoch, best_eval = save_best_model(
            model, optimizer, dev_eval, epoch + 1, best_eval, best_epoch, uid
        )

    train_stats.append(
        {
            "train_results": train_results,
            "train_module_loss": train_module_loss,
            "dev_eval": dev_eval,
        }
    )

    if not config.lottery_ticket and epoch > 0:
        model, _, epoch = load_checkpoint("./checkpoint/ckpt.{}.pth".format(uid), model)
    else:
        for module in main_loop_modules:
            module.pre_epoch(model, True, epoch + 1, optimizer=optimizer, lr_scheduler=lr_scheduler)

    # test the final model with noise on the dev-set
    # test the final model on the test set
    test_results_dict, dev_final_results_dict = {}, {}
    for k in val_keys:
        if "img_classification" not in k:
            dev_final_results = test_neural_model(
                model,
                data_loader=dataloaders["validation"][k],
                device=device,
                epoch=epoch,
                eval_type="Validation",
            )
            test_results = test_neural_model(
                model,
                data_loader=dataloaders["test"][k],
                device=device,
                epoch=epoch,
                eval_type="Test",
            )
            dev_final_results_dict.update(dev_final_results)
            test_results_dict.update(test_results)
        else:
            if "rep_matching" not in k:
                dev_final_results = test_model(
                    model=model,
                    epoch=epoch,
                    n_iterations=val_n_iterations[k],
                    criterion=get_subdict(criterion, [k]),
                    device=device,
                    data_loader=get_subdict(dataloaders["validation"], [k]),
                    config=config,
                    noise_test=True,
                    seed=seed,
                )
                dev_final_results_dict.update(dev_final_results)

            test_results = test_model(
                model=model,
                epoch=epoch,
                n_iterations=test_n_iterations[k],
                criterion=get_subdict(criterion, [k]),
                device=device,
                data_loader=get_subdict(dataloaders["test"], [k]),
                config=config,
                noise_test=False,
                seed=seed,
                eval_type="Test",
            )
            test_results_dict.update(test_results)
            dev_final_results_dict.update(dev_final_results)

    final_results = {
        "test_results": test_results_dict,
        "dev_eval": best_eval,
        "epoch": best_epoch,
        "dev_final_results": dev_final_results_dict,
    }

    if "c_test" in dataloaders:
        test_c_results = {}
        for k in val_keys:
            if "rep_matching" not in k:
                for c_category in list(dataloaders["c_test"][k].keys()):
                    test_c_results[c_category] = {}
                    for c_level, dataloader in dataloaders["c_test"][k][
                        c_category
                    ].items():
                        results = test_model(
                            model=model,
                            n_iterations=len(dataloader),
                            epoch=epoch,
                            criterion=get_subdict(criterion, [k]),
                            device=device,
                            data_loader={k: dataloader},
                            config=config,
                            noise_test=False,
                            seed=seed,
                            eval_type="Test-C",
                        )
                        test_c_results[c_category][c_level] = results
        final_results["test_c_results"] = test_c_results

    if "st_test" in dataloaders:
        test_st_results = test_model(
            model=model,
            epoch=epoch,
            n_iterations=len(dataloaders['st_test']),
            criterion=get_subdict(criterion, ["img_classification"]),
            device=device,
            data_loader={"img_classification": dataloaders['st_test']},
            config=config,
            noise_test=False,
            seed=seed,
            eval_type="Test-ST",
        )
        final_results["test_st_results"] = test_st_results
    return (
        test_results_dict[list(config.loss_functions.keys())[0]]["eval"],
        (train_stats, final_results),
        model.state_dict(),
    )
