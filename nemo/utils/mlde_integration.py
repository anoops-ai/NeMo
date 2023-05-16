from argparse import Namespace
from attrdict import AttrDict
from dataclasses import dataclass
import glob
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, cast, Dict, Iterator, List, Optional, Tuple, Type, Union, Iterable


import determined as det
from determined._info import ClusterInfo
from determined.core._searcher import SearcherOperation
import pytorch_lightning as pl
from pytorch_lightning.utilities.distributed import rank_zero_only
import lightning
import torch
from nemo.utils.exp_manager import NeMoModelCheckpoint
from nemo.utils.app_state import AppState
from nemo.collections.common.callbacks import EMA


import shutil
from omegaconf import DictConfig, OmegaConf, open_dict


def flatten(xs: List[List]) -> List:
    return [item for items in xs for item in items]


def get_cluster_info_with_assert() -> ClusterInfo:
    """
    Raise an exception if not run on a Determined cluster.  Returns ClusterInfo.
    """
    info = det.get_cluster_info()
    assert info, "This code can only be run on-cluster."
    return info

def get_checkpoint_metadata(core_context: det.core.Context) -> Optional[Dict]:
    info = det.get_cluster_info()
    if info:
        ckpt_id = info.latest_checkpoint
        if ckpt_id:
            return cast(Dict, core_context.checkpoint.get_metadata(ckpt_id))
    return None


def get_searcher_metric() -> str:
    return cast(str, get_cluster_info_with_assert().trial._config["searcher"]["metric"])


def get_searcher_max_length() -> int:
    max_length_entry = get_cluster_info_with_assert().trial._config["searcher"]["max_length"]
    if isinstance(max_length_entry, dict):
        assert tuple(max_length_entry.keys()) == (
            "epochs",
        ), "Must express max training length in epochs."
        return cast(int, max_length_entry["epochs"])
    else:
        return cast(int, max_length_entry)


@dataclass
class DeterminedIntegrationSharedState:
    """
    State shared between the components of the Determined integration on a single Trainer.
    """

    core_context: det.core.Context
    searcher_ops: Iterator[SearcherOperation]
    current_op: SearcherOperation
    global_step: int = 0
    last_metric: Optional[float] = None


# Default environment settings in PTL don't work with multi-node DeepSpeed launch, so we
# need to explicitly configure this.
class DeterminedClusterEnvironment(
    lightning.fabric.plugins.environments.cluster_environment.ClusterEnvironment  # type: ignore
):
    def __init__(self, shared: DeterminedIntegrationSharedState):
        self.shared = shared

    @property
    def creates_processes_externally(self) -> bool:
        return True

    @property
    def main_address(self) -> str:
        return os.environ["DET_CHIEF_IP"]

    @property
    def main_port(self) -> int:
        if "USE_DEEPSPEED" in os.environ:
            # Determined uses the default port for DeepSpeed init_distributed:
            # - https://deepspeed.readthedocs.io/en/latest/initialize.html
            return 29500
        else:
            return int(os.environ["MASTER_PORT"])

    @staticmethod
    def detect() -> bool:
        raise Exception("Unimplemented")

    def world_size(self) -> int:
        return self.shared.core_context.distributed.size

    def set_world_size(self, size: int) -> None:
        assert size == self.shared.core_context.distributed.size

    def global_rank(self) -> int:
        return self.shared.core_context.distributed.rank

    def set_global_rank(self, rank: int) -> None:
        assert rank == self.shared.core_context.distributed.rank

    def local_rank(self) -> int:
        return self.shared.core_context.distributed.local_rank

    def node_rank(self) -> int:
        return self.shared.core_context.distributed.cross_rank


class DeterminedLogger(pl.loggers.logger.Logger):  # type: ignore
    def __init__(self, shared: DeterminedIntegrationSharedState) -> None:
        self.shared = shared

    def log_hyperparams(
        self, params: Union[Dict[str, Any], Namespace], *args: Any, **kwargs: Any
    ) -> None:
        pass

    @rank_zero_only  # type: ignore
    def log_metrics(self, metrics: Dict, step: int) -> None:
        searcher_metric = get_searcher_metric()
        if searcher_metric in metrics:
            self.shared.last_metric = metrics[searcher_metric]

    @property
    def name(self) -> Optional[str]:
        pass

    @property
    def version(self) -> Optional[Union[int, str]]:
        pass


def upload_determined_checkpoint(
    path: Union[str, Path], shared: DeterminedIntegrationSharedState
) -> None:
    if shared.core_context.distributed.rank == 0:
        det_checkpoint_metadata = {
            "steps_completed": shared.global_step,
            "trial_id": get_cluster_info_with_assert().trial.trial_id,
        }

        # TBR
        # move to singleton
        info = det.get_cluster_info()
        mlde_ckpt_path = os.path.join(info.trial._config['checkpoint_storage']['host_path'], info.trial._config['checkpoint_storage']['storage_path'])
        with shared.core_context.checkpoint.store_path(det_checkpoint_metadata) as (_, dest_uuid):
                ckpt_name = os.path.basename(os.path.normpath(path))
                dst_ckpt_path = os.path.join(mlde_ckpt_path,dest_uuid,ckpt_name)
                print (f'ckpt_name = {ckpt_name}, src_path = {path}, dst = {dst_ckpt_path}')
                os.rename(path, dst_ckpt_path)
        # if os.path.isfile(path):
        #     print ('dbg----upload_determined_checkpoint path =',path)
        #     # Create a temporary directory with a symbolic link to the saved file,
        #     # so we can upload it without making a copy.
        #     # If path is a directory terminated with /, basename will return empty string --
        #     # we use normpath to ensure it returns the last directory.
        #     ckpt_name = os.path.basename(os.path.normpath(path))
        #     with tempfile.TemporaryDirectory() as temp_dir:
        #         temp_ckpt_path = os.path.join(temp_dir, ckpt_name)
        #         print ('dbg----upload_determined_checkpoint temp_ckpt_path =',temp_ckpt_path)
        #         os.symlink(os.path.abspath(path), os.path.abspath(temp_ckpt_path))
        #         shared.core_context.checkpoint.upload(temp_dir, det_checkpoint_metadata)
        # else:
        #     shared.core_context.checkpoint.upload(path, det_checkpoint_metadata)


class DeterminedCheckpointIO(pl.plugins.io.CheckpointIO):  # type: ignore
    def __init__(
        self,
        shared: DeterminedIntegrationSharedState,
        base_ckpt_io: Optional[pl.plugins.io.CheckpointIO],
    ) -> None:

        self.shared = shared
        if base_ckpt_io:
            self.base_ckpt_io = base_ckpt_io
        else:
            self.base_ckpt_io = pl.plugins.io.TorchCheckpointIO()

    def save_checkpoint(
        self,
        checkpoint: Dict[str, Any],
        path: Union[str, Path],
        storage_options: Optional[Any] = None,
    ) -> None:
        print('dbg---- save_checkpoint')
        self.base_ckpt_io.save_checkpoint(checkpoint, path, storage_options)
        upload_determined_checkpoint(path, self.shared)

    def load_checkpoint(
        self,
        path: Union[str, Path],
        map_location: Optional[Callable] = lambda storage, loc: storage,
    ) -> Dict[str, Any]:
        print('dbg---- load_checkpoint', path)
        return cast(Dict[str, Any], self.base_ckpt_io.load_checkpoint(path, map_location))

    def remove_checkpoint(self, path: Union[str, Path]) -> None:
        print('dbg---- remove_checkpoint', path)
        self.base_ckpt_io.remove_checkpoint(path)


class DeterminedCallback(pl.callbacks.Callback):  # type: ignore
    def __init__(self, shared: DeterminedIntegrationSharedState) -> None:
        self.shared = shared
        self.core_context = shared.core_context
        self.val_epoch_outputs: List[pl.utilities.types.STEP_OUTPUT] = []
        self.test_epoch_outputs: List[pl.utilities.types.STEP_OUTPUT] = []

    def setup(
        self, trainer: pl.Trainer, pl_module: pl.LightningModule, stage: Optional[str] = None
    ) -> None:
        # If fitting/testing multiple times, keep a monotonically increasing global step for
        # reporting Determined metrics and checkpoints.
        self.shared.global_step += 1
        self.initial_global_step = self.shared.global_step

    def on_train_batch_end(
        self,
        trainer: pl.Trainer,
        pl_module: pl.LightningModule,
        outputs: pl.utilities.types.STEP_OUTPUT,
        batch: Any,
        batch_idx: int,
    ) -> None:
        outputs = cast(Dict[str, Any], outputs)
        self.shared.global_step = self.initial_global_step + trainer.global_step
        if self.core_context.distributed.rank == 0:
            outputs = {k: v.item() if isinstance(v, torch.Tensor) else v for k, v in outputs.items()}
            # We only report training metrics from rank 0 to avoid too many blocking syncs.
            self.core_context.train.report_training_metrics(
                steps_completed=self.shared.global_step, metrics=outputs
            )

    def on_validation_epoch_start(self, trainer: pl.Trainer, pl_module: pl.LightningModule) -> None:
        self.val_epoch_outputs = []

    def on_validation_batch_end(
        self,
        trainer: pl.Trainer,
        pl_module: pl.LightningModule,
        outputs: Optional[pl.utilities.types.STEP_OUTPUT],
        batch: Any,
        batch_idx: int,
        dataloader_idx: int,
    ) -> None:
        if outputs:
            outputs = cast(Dict[str, Any], outputs)
            self.val_epoch_outputs.append({k: v.item() for k, v in outputs.items()})

    def on_validation_epoch_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule) -> None:
        outputs = self.core_context.distributed.gather(self.val_epoch_outputs)
        self.val_epoch_outputs = []
        if self.core_context.distributed.rank == 0:
            outputs = flatten(cast(List[List[pl.utilities.types.STEP_OUTPUT]], outputs))
            if outputs:
                avg_results = {
                    k: sum([x[k] for x in outputs]) / len(outputs) for k in outputs[0].keys()
                }
                self.core_context.train.report_validation_metrics(
                    steps_completed=self.shared.global_step, metrics=avg_results
                )
        if self.core_context.distributed.rank == 0:
            self.shared.current_op.report_progress(trainer.current_epoch + 1)
        if (trainer.current_epoch + 1) >= self.shared.current_op.length:
            if self.core_context.distributed.rank == 0:
                if self.shared.last_metric is None:
                    logging.warning(
                        f"Searcher metric {get_searcher_metric()} was not "
                        "logged.  Reporting as 0.",
                    )
                    self.shared.current_op.report_completed(0)
                else:
                    self.shared.current_op.report_completed(self.shared.last_metric)
            try:
                self.shared.current_op = next(self.shared.searcher_ops)
            except StopIteration:
                logging.info("Reached end of searcher operations.")
                trainer.should_stop = True
        if self.core_context.preempt.should_preempt():
            print ('dbg---- prempt_should_prempt() got notified, exiting')
            exit(0)

    def on_test_epoch_start(self, trainer: pl.Trainer, pl_module: pl.LightningModule) -> None:
        self.test_epoch_outputs = []

    def on_test_batch_end(
        self,
        trainer: pl.Trainer,
        pl_module: pl.LightningModule,
        outputs: Optional[pl.utilities.types.STEP_OUTPUT],
        batch: Any,
        batch_idx: int,
        dataloader_idx: int,
    ) -> None:
        if outputs:
            self.test_epoch_outputs.append({k: v.item() for k, v in outputs.items()})

    def on_test_epoch_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule) -> None:
        outputs = self.core_context.distributed.gather(self.test_epoch_outputs)
        self.test_epoch_outputs = []
        if self.core_context.distributed.rank == 0:
            outputs = flatten(cast(List[List[pl.utilities.types.STEP_OUTPUT]], outputs))
            if outputs:
                avg_results = {
                    k: sum([x[k] for x in outputs]) / len(outputs) for k in outputs[0].keys()
                }
                self.core_context.train.report_validation_metrics(
                    steps_completed=self.shared.global_step, metrics=avg_results
                )



def get_hyperparameters() -> AttrDict:
    """
    Returns Determined trial hyperparameters as an AttrDict.
    """
    info = det.get_cluster_info()
    assert info is not None, "This example only runs on-cluster"
    return cast(Dict, AttrDict(info.trial.hparams))


def determined_core_init() -> det.core.Context:
    """
    Initializes a det.core.Context appropriately.
    """

    distributed_context = det.core.DistributedContext.from_torch_distributed()
    return det.core.init(distributed=distributed_context)


def _add_integration_controlled_args(kwargs: Dict[str, Any], intargs: Dict[str, Any]) -> None:
    """
    Adds arguments to kwargs after asserting they're not present.
    """
    for k in intargs:
        assert (
            k not in kwargs
        ), f"`{k}` is supplied by build_determined_trainer, so can not be as an argument."
        kwargs[k] = intargs[k]


def _append_integration_controlled_args(kwargs: Dict[str, Any], intargs: Dict[str, Any]) -> None:
    """
    Appends the value in intargs to the associated list in kwargs, creating if necessary.
    """
    for k in intargs:
        val = kwargs.get(k, [])
        if not (isinstance(val, list)):
            val = [val]
        val.append(intargs[k])
        kwargs[k] = val


def build_determined_trainer(
    core_context: det.core.Context,
    strategy: Any,
    cfg_exp_manager: Any,
    base_ckpt_io: Optional[pl.plugins.io.CheckpointIO] = None,
    **kwargs: Any,
#) -> Tuple[pl.Trainer, pl.LightningModule]:
) -> pl.Trainer:
    """
    Returns a tuple of (Trainer, LightningModule) configured to run under Determined.
    The trainer and module state will be loaded from checkpoint if resumed from a pause.
    The module state will be loaded from checkpoint if this is a new trial with
    a checkpoint supplied (e.g. Continue Trial in the Web UI).

    Accepts the usual parameters to Trainer(...), with the following exceptions controlled
    by the Determined trial configuration:
    - num_nodes
    - devices
    - accelerator
    - resume_from_checkpoint
    - max_epochs
    """    

    searcher_ops = core_context.searcher.operations()
    shared = DeterminedIntegrationSharedState(
        core_context=core_context,
        searcher_ops=searcher_ops,
        current_op=next(searcher_ops),
    )

    _append_integration_controlled_args(
        kwargs,
        {
            "callbacks": DeterminedCallback(shared),
            "logger": DeterminedLogger(shared),
             "plugins": DeterminedCheckpointIO(shared, base_ckpt_io),
        },
    )
    _add_integration_controlled_args(
        kwargs,
        {
            "num_nodes": core_context.distributed.cross_size,
            "devices": "auto",
            "accelerator": "gpu",
            #"resume_from_checkpoint": None if module_load_only else ckpt_path,
            "max_epochs": get_searcher_max_length(),
        },
    )


    info = det.get_cluster_info()
    assert info is not None, "this example only runs on-cluster"


    # TBR
    # MLDE checkpointing hack - using it as file share for phase 1 integration
    base_ckpt_dir = os.path.join(info.trial._config['checkpoint_storage']['host_path'], info.trial._config['checkpoint_storage']['storage_path'])
    exp_dir = os.path.join(base_ckpt_dir, 'exp_'+str(info.trial.trial_id))
    mlde_ckpt_dirs = Path(base_ckpt_dir)

    # TBR get it from bootstrap? it is hard coded inside of exp_manager or add a fn to expose it
    nemo_ckpt_dir = os.path.join(exp_dir, 'nemo_workdir', 'None', 'version_None', 'checkpoints')


    # Hack as Nemo needs a exp_dir from which log_dir is derived that needs 
    cfg_exp_manager.exp_dir = os.path.join(exp_dir, 'nemo_workdir')
    print ('dbg--- setting cfg exp_dir = ', cfg_exp_manager.exp_dir)

    # Hack set exp_dir which is required in resumption
    latest_checkpoint = info.latest_checkpoint
    print (f'dbg--- latest_checkpoint = {latest_checkpoint}')
    if latest_checkpoint != None:
        OmegaConf.set_struct(cfg_exp_manager, True)
        with open_dict(cfg_exp_manager):
            cfg_exp_manager.resume_if_exists = True
        print (f'dbg--- setting resume as we found a checkpoint')

    



    # # clean up all the symlinks saved before starting/resuming training
    # if os.path.exists(nemo_ckpt_dir):
    #     shutil.rmtree(nemo_ckpt_dir)
    # os.makedirs(nemo_ckpt_dir, exist_ok=True)

    # tmp to get the last checkpoint
    last_checkpoints = list(mlde_ckpt_dirs.rglob("*last.ckpt"))

    if len(last_checkpoints) > 0:

        print('dbg---- checkpoints = ', last_checkpoints)
        print ('dbg--- latest ckpt = ', last_checkpoints[0])

        # MLDE need to do this currently due to mlde ckpting logic
        ckpt_name = os.path.basename(os.path.normpath(last_checkpoints[0]))

        dst_ckpt = os.path.join(nemo_ckpt_dir, ckpt_name)

        os.symlink(last_checkpoints[0], dst_ckpt)

        print(f"dbg--- Symbolic link created successfully for src = {last_checkpoints[0]} to dst = {dst_ckpt}")


    return pl.Trainer(strategy=strategy, **kwargs)