from icevision.imports import *
from icevision.core import *
from icevision.core.tasks import Task
from torch.utils.data import Dataset
from icevision.data.dataset import Dataset as RecordDataset
from icevision.utils.utils import normalize, flatten

import icevision.tfms as tfms
import torchvision.transforms as Tfms

__all__ = ["HybridAugmentationsRecordDataset", "RecordDataset"]


class HybridAugmentationsRecordDataset(Dataset):
    """
    Dataset that stores records internally and dynamically attaches an `img` component
    to each task when being fetched

    Arguments:
        * records: A list of records.
        * classification_transforms_groups <Dict[str, Dict[str, Union[Tfms.Compose, List[str]]]] : a dict
            that creates groups of tasks, where each task receives the same transforms and gets a dedicated
            forward pass in the network. For example:
                dict(
                    tasks=["shot_framing", "color_tones"],
                    transforms=Tfms.Compose([Tfms.Resize(224), Tfms.ToTensor()])
                )
        * detection_transforms <tfms.A.Adapter> - Icevision albumentations adapter for detection transforms
        * norm_mean <List[float]> : norm mean stats
        * norm_std <List[float]> : norm stdev stats
        * debug <bool> : If true, prints info & unnormalised `PIL.Image`s are returned on fetching items
    """

    def __init__(
        self,
        records: List[dict],
        classification_transforms_groups: dict,
        detection_transforms: Optional[tfms.Transform] = None,
        norm_mean: Collection[float] = [0.485, 0.456, 0.406],
        norm_std: Collection[float] = [0.229, 0.224, 0.225],
        debug: bool = False,
    ):
        "Return `PIL.Image` when `debug=True`"
        self.records = records
        self.classification_transforms_groups = classification_transforms_groups
        self.detection_transforms = detection_transforms
        self.norm_mean = norm_mean
        self.norm_std = norm_std
        self.debug = debug
        self.validate()

    def validate(self):
        """
        Input args validation
        * Ensure that each value in the `classification_transforms_groups` dict
          has a "tasks" and "transforms" key
        * Ensure the number of tasks mentioned in `classification_transforms_groups`
          match up _exactly_ with the tasks in the record
        """
        for group in self.classification_transforms_groups.values():
            assert set(group.keys()).issuperset(
                ["tasks", "transforms"]
            ), f"Invalid keys in `classification_transforms_groups`"

        missing_tasks = []
        for attr in flatten(
            [g["tasks"] for g in self.classification_transforms_groups.values()]
        ):
            if not hasattr(self.records[0], attr):
                missing_tasks += [attr]
        if not missing_tasks == []:
            raise ValueError(
                f"`classification_transforms_groups` has more groups than are present in the `record`. \n"
                f"Missing the following tasks: {missing_tasks}"
            )

    def __len__(self):
        return len(self.records)

    def __getitem__(self, i):
        record = self.records[i].load()

        # Keep a copy of the orig img as it gets modified by albu
        original_img = deepcopy(record.img)
        if isinstance(original_img, np.ndarray):
            original_img = PIL.Image.fromarray(original_img)

        # Do detection transform and assign it to the detection task
        if self.detection_transforms is not None:
            record = self.detection_transforms(record)

        record.add_component(ImageRecordComponent(Task("detection")))
        record.detection.set_img(record.img)

        if self.debug:
            print(f"Fetching Item #{i}")

        # Do classification transforms
        for group in self.classification_transforms_groups.values():
            img_tfms = group["transforms"]
            tfmd_img = img_tfms(original_img)
            if self.debug:
                print(f"  Group: {group['tasks']}, ID: {id(tfmd_img)}")

            # NOTE:
            # * We need to add the img component dynamically here to
            #   play nice with the albumentations adapter 🤬
            # * Setting the same img twice (to diff parts in memory),
            #   but it's ok cuz we will unload the record in DataLoader
            for task in group["tasks"]:
                record.add_component(ImageRecordComponent(Task(task)))
                getattr(record, task).set_img(tfmd_img)
                if self.debug:
                    print(f"   - Task: {task}, ID: {id(tfmd_img)}")

        # This is a bit verbose, but allows us to return PIL images for easy debugging.
        # Else, it returns normalized numpy arrays, like usual icevision datasets
        for comp in record.components:
            if isinstance(comp, ImageRecordComponent):
                # Convert to `np.ndarray` if it isn't already
                if isinstance(comp.img, PIL.Image.Image):
                    comp.set_img(np.array(comp.img))
                if self.debug:  # for debugging only
                    comp.set_img(PIL.Image.fromarray(comp.img))
                else:
                    comp.set_img(
                        normalize(comp.img, mean=self.norm_mean, std=self.norm_std)
                    )

        return record

    def __repr__(self):
        return f"<{self.__class__.__name__} with {len(self.records)} items and {len(self.group_tfms)+1} groups>"