# Modified from `icevision.models.mmdet.lightning.model_adapter`
# NOTE `torchmetrics` comes installed with `pytorch-lightning`
# We could in theory also do `pl.metrics`

# import pytorch_lightning.metrics as tm
import torchmetrics as tm
from icevision.all import *
from mmcv.utils import ConfigDict
from loguru import logger
from icevision.models.multitask.mmdet.single_stage import ForwardType
from icevision.models.multitask.mmdet.prediction import *


__all__ = ["HybridSingleStageDetectorLightningAdapter"]


class HybridSingleStageDetectorLightningAdapter(pl.LightningModule, ABC):
    """Lightning module specialized for EfficientDet, with metrics support.

    The methods `forward`, `training_step`, `validation_step`, `validation_epoch_end`
    are already overriden.

    # Arguments
        model: The pytorch model to use.
        metrics: `Sequence` of metrics to use.

    # Returns
        A `LightningModule`.
    """

    def __init__(
        self,
        model: nn.Module,
        metrics: List[Metric] = None,
        debug: bool = False,
    ):
        super().__init__()
        self.metrics = metrics or []
        self.model = model
        self.debug = debug

        self.classification_metrics = {}
        for name, head in model.classifier_heads.items():
            if head.multilabel:
                thresh = head.thresh if head.thresh is not None else 0.5
                metric = tm.Accuracy(threshold=thresh, subset_accuracy=True)
            else:
                metric = tm.Accuracy(threshold=0.01, top_k=1)
            setattr(self, f"{name}_accuracy", metric)
        self.post_init()

    def post_init(self):
        pass

    # ======================== TRAINING METHODS ======================== #

    def forward(self, *args, **kwargs):
        return self.model(*args, **kwargs)

    def training_step(self, batch: Tuple[dict, Sequence[RecordType]], batch_idx):
        # Unpack batch into dict + list of records
        data, samples = batch
        # Get model outputs - dict of losses and vars to log
        step_type = ForwardType.TRAIN_MULTI_AUG
        if "img_metas" in data.keys():
            step_type = ForwardType.TRAIN

        if self.debug:
            logger.info(f"Training Step: {data.keys()}")
            logger.info(f"Batch Idx: {batch_idx}")
            logger.info(f"Training Mode: {step_type}")

        outputs = self.model.train_step(data=data, step_type=step_type)

        # Log losses
        self._log_vars(outputs["log_vars"], "train")

        # NOTE: outputs["loss"] is not scaled in distributed training... ?
        # Maybe we should return `outputs["log_vars"]["loss"]` instead?
        return outputs["loss"]

    def validation_step(self, batch, batch_idx):
        data, records = batch
        if self.debug:
            logger.info(f"Validation Step: {data.keys()}")
            logger.info(f"Batch Idx: {batch_idx}")

        self.model.eval()
        with torch.no_grad():
            # get losses
            outputs = self.model.train_step(data=data, step_type=ForwardType.TRAIN)
            raw_preds = self.model(data=data, forward_type=ForwardType.EVAL)
            self.log_classification_metrics(
                classification_preds=raw_preds["classification_results"],
                yb_classif=data["gt_classification_labels"],
            )

        preds = self.convert_raw_predictions(
            batch=data, raw_preds=raw_preds, records=records
        )
        self.accumulate_metrics(preds)
        self._log_vars(outputs["log_vars"], "valid")

        # TODO: is train and eval model automatically set by lighnting?
        self.model.train()

    # ======================== LOGGING METHODS ======================== #

    def convert_raw_predictions(self, batch, raw_preds, records):
        classification_configs = {
            name: ConfigDict(
                multilabel=head.multilabel, topk=head.topk, thresh=head.thresh
            )
            for name, head in self.model.classifier_heads.items()
        }
        return convert_raw_predictions(
            batch=batch,
            raw_preds=raw_preds,
            records=records,
            detection_threshold=0.0,
            classification_configs=classification_configs,
        )

    def log_classification_metrics(
        self,
        classification_preds: Dict[str, Tensor],
        yb_classif: Dict[str, Tensor],
        on_step: bool = False,
        prefix: str = "valid",
    ):
        prefix = f"{prefix}_" if not prefix == "" else ""
        for name in self.model.classifier_heads.keys():
            # for name, metric in self.classification_metrics.items():
            metric = getattr(self, f"{name}_accuracy")
            self.log(
                f"{prefix}{metric.__class__.__name__.lower()}__{name}",  # accuracy__shot_framing
                # metric(classification_preds[name], yb_classif[name]),
                metric(classification_preds[name], yb_classif[name].type(torch.int)),
                on_step=on_step,
                on_epoch=True,
            )

    def _log_vars(self, log_vars: dict, mode: str):
        for k, v in log_vars.items():
            self.log(f"{mode}/{k}", v.item() if isinstance(v, torch.Tensor) else v)

    def validation_epoch_end(self, outs):
        self.finalize_metrics()

    def accumulate_metrics(self, preds):
        for metric in self.metrics:
            metric.accumulate(preds=preds)

    def finalize_metrics(self) -> None:
        for metric in self.metrics:
            metric_logs = metric.finalize()
            for k, v in metric_logs.items():
                self.log(f"{metric.name}/{k}", v)
