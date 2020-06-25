from nnfabrik.main import *
from nnfabrik.template import *
from .collapse import Collapsed


@schema
class TrainedModel(TrainedModelBase):
    table_comment = "My Trained models"


@schema
class CollapsedTrainedModel(Collapsed):
    Source = TrainedModel()
