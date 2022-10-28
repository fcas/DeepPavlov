# Copyright 2022 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
import numpy as np
import copy
from logging import getLogger
from collections import defaultdict
from typing import Iterator, List, Optional, Tuple, Union, Dict

from deeppavlov.core.data.data_learning_iterator import DataLearningIterator
from deeppavlov.core.common.params import from_params
from deeppavlov.core.common.registry import register


log = getLogger(__name__)


@register('multitask_iterator')
class MultiTaskIterator:
    """
    Class merges data from several dataset iterators. When used for batch generation batches from
    merged dataset iterators are united into one batch. If sizes of merged datasets are different
    smaller datasets are repeated until their size becomes equal to the largest dataset.

    Args:
        data: dictionary which keys are task names and values are dictionaries with fields
            ``"train", "valid", "test"``.
        num_train_epochs: number of training epochs
        tasks: dictionary which keys are task names and values are init params of dataset iterators.
        batch_size: batch_size
        sampling_mode: mode of sampling we use. It can be plain, uniform or anneal.
        gradient_accumulation_steps: number of gradient accumulation steps. Default is 1
        steps_per_epoch: number of steps per epoch. Nesessary if gradient_accumulation_steps > 1
        iterator_class_name: name of iterator class.
        use_label_name, seed, features - parameters for the iterator class
        one_element_tuples: if True, tuple of x consisting of one element is returned in this element. Default: True


    Attributes:
        data: dictionary of data with fields "train", "valid" and "test" (or some of them)
    """

    def __init__(
            self,
            data: dict,
            num_train_epochs: int,
            tasks: dict,
            batch_size: int = 8,
            sampling_mode='plain',
            gradient_accumulation_steps: Optional[int] = 1,
            steps_per_epoch: int = 0,
            iterator_class_name=None,
            use_label_name=False,
            seed=42,
            features=None,
            one_element_tuples=True,
            *args,
            **kwargs
    ):
        self.task_iterators = {}
        for task_name, task_iterator_params in tasks.items():
            task_iterator_params = copy.deepcopy(task_iterator_params)
            for param_name in ['use_label_name', 'seed', 'features', 'iterator_class_name']:
                if param_name not in task_iterator_params:
                    if param_name != 'features':
                        error_msg = f'Set {param_name} either in scope for {param_name} or as default_params'
                        assert eval(param_name) is not None, error_msg
                        log.info(
                            f'Using {param_name} as set on the reader level')
                        if param_name != 'iterator_class_name':
                            task_iterator_params[param_name] = eval(param_name)
                        else:
                            task_iterator_params['class_name'] = iterator_class_name
                    elif param_name == 'features':
                        log.warning(
                            'Features nos specified. Experimentally not passing it on as a param')
                elif param_name == 'iterator_class_name' and 'iterator_class_name' in task_iterator_params:
                    task_iterator_params["class_name"] = task_iterator_params[
                        "iterator_class_name"]
                    del task_iterator_params["iterator_class_name"]
            self.task_iterators[task_name] = from_params(
                task_iterator_params, data=data[task_name]
            )
        self.n_tasks = len(tasks.keys())
        self.num_train_epochs = num_train_epochs
        self.steps_per_epoch = steps_per_epoch
        self.gradient_accumulation_steps = gradient_accumulation_steps
        self.epochs_done = 0
        self.steps_taken = 0
        self.task_id = None
        self.sampling_mode = sampling_mode
        self.chosen_batchs = defaultdict(int)
        self.data = {
            "train": self._extract_data_type("train"),
            "valid": self._extract_data_type("valid"),
            "test": self._extract_data_type("test"),
        }
        for mode in ["train", "valid", "test"]:
            log.info(f'For {mode}')
            for task_name in self.data[mode]:
                log.info(
                    f'{task_name} has {len(self.data[mode][task_name])} examples')
        self.train_sizes = self._get_data_size("train")
        assert self.train_sizes
        if steps_per_epoch == 0:
            self.steps_per_epoch = sum(self.train_sizes) // batch_size
        else:
            self.steps_per_epoch = steps_per_epoch

        def is_nan(a): return a != a
        for mode in ['train', 'valid', 'test']:
            for task in self.data[mode]:
                for i in range(len(self.data[mode][task]) - 1, -1, -1):
                    x = self.data[mode][task][i][0]
                    y = self.data[mode][task][i][1]
                    if is_nan(x) or any([is_nan(z) for z in x]) or is_nan(y):
                        del self.data['train'][task][i]
                        log.info(
                            f'NAN for mode {mode} task {task} element {i} CLEARED')
                        breakpoint()
                    elif isinstance(x, tuple) and len(x) == 1 and one_element_tuples:
                        # x is a tuple consisting of 1 element. return it as string
                        self.data[mode][task][i] = (x[0], y)
        self.max_task_data_len = dict()
        for data_type in self.data:
            sizes = self._get_data_size(data_type)
            self.max_task_data_len[data_type] = max(sizes)
        self.sample_x_instances = None
        self.sample_y_instances = None

    def _get_data_size(self, data_type):
        """
        Returns list of sizes of each dataset for the given data_type: train,test or valid.
        """

        return [len(self.data[data_type][key]) for key in self.data[data_type]]

    def _get_probs(self, data_type):
        """
        Returns sampling probabilities for different sampling modes - plain, uniform or anneal
        """

        assert data_type in self.data
        curr_data = self.data[data_type]

        if self.sampling_mode == 'uniform':
            sizes = [1 for _ in self._get_data_size(data_type)]
            # as we sample uniformly
            s = sum(sizes)
            probs = [p / s for p in sizes]
        elif self.sampling_mode == 'plain':
            sizes = self._get_data_size(data_type)
            n_samples = sum(sizes)
            probs = [p / n_samples for p in sizes]
        elif self.sampling_mode == 'plainiform':
            sizes = self._get_data_size(data_type)
            n_samples = sum(sizes)
            probs_plain = [p / n_samples for p in sizes]
            probs_uniform = [1 / len(sizes) for _ in sizes]
            probs = [0.5*(prob_plain + prob_uniform) 
                     for prob_plain, prob_uniform in zip(probs_plain, probs_uniform)]
            probs = [k / sum(probs) for k in probs]
        elif self.sampling_mode == 'anneal':
            alpha = 1.0 - 0.8 * (self.epochs_done / self.num_train_epochs)
            annealed_sizes = [
                p ** alpha for p in self._get_data_size(data_type)]
            n_samples = sum(annealed_sizes)
            probs = [p / n_samples for p in annealed_sizes]
        else:
            raise Exception(f'Unsupported sampling mode {self.sampling_mode}')
        return probs

    def _extract_data_type(self, data_type):
        """
        Function that merges data of the current daata_type(e.g train) from all task_iterators into one dict
        """

        dataset_part = {}
        for task, iterator in self.task_iterators.items():
            dataset_part[task] = getattr(iterator, data_type)
        return dataset_part

    def _transform_before_yielding(self, x, y, batch_size):
        """
        Function that transforms data from dataset before yielding
        """

        assert len(x) == len(y)
        new_x, new_y = [], []
        for i in range(batch_size):
            x_tuple = tuple([x[id][i] for id in range(self.n_tasks)])
            y_tuple = tuple([y[id][i] for id in range(self.n_tasks)])
            if self.n_tasks == 1:
                x_tuple = x_tuple[0]
                y_tuple = y_tuple[0]
            new_x.append(x_tuple)
            new_y.append(y_tuple)
        batchs = (tuple(new_x), tuple(new_y))
        return batchs

    def gen_batches(self, batch_size: int, data_type: str = "train",
                    shuffle: bool = None) -> Iterator[Tuple[tuple, tuple]]:
        """
        Generates batches and expected output to train neural networks.
        If there are not enough samples forom any task, samples are padded with None

        Args:
            batch_size: number of samples in batch
            data_type: can be either 'train', 'test', or 'valid'
            shuffle: whether to shuffle dataset before batching

        Yields:
            A tuple of a batch of inputs and a batch of expected outputs.
            Inputs and outputs are tuples. Element of inputs or outputs is a tuple which
            elements are x values of merged tasks in the ordertasks are present in
            `tasks` argument of `__init__` method.
        """

        max_task_data_len = self.max_task_data_len[data_type]
        size_of_last_batch = max_task_data_len % batch_size
        if size_of_last_batch == 0:
            size_of_last_batch = batch_size
        log.info(f'Batch size {batch_size} with gradient accumulation steps {self.gradient_accumulation_steps}')
        log.info(f'Efficient batch size {batch_size//self.gradient_accumulation_steps}')
        batch_size = batch_size // self.gradient_accumulation_steps
        n_batches = math.ceil(max_task_data_len / batch_size)

        if data_type == "train":
            generators = [
                SingleTaskBatchGenerator(iter_, batch_size, data_type, shuffle)
                for iter_ in self.task_iterators.values()
            ]
            # probs only required while training
            probs = self._get_probs("train")
            for step in range(self.steps_per_epoch):

                if (self.steps_taken + 1) % self.gradient_accumulation_steps == 0 or self.task_id is None:
                    self.task_id = np.random.choice(self.n_tasks, p=probs)
                    self.chosen_batchs[self.task_id] += 1
                x = [[None for _ in range(batch_size)]
                     for task_id in range(self.n_tasks)]
                y = [[None for _ in range(batch_size)]
                     for task_id in range(self.n_tasks)]
                x[self.task_id], y[self.task_id] = generators[self.task_id].__next__()
                if not all([s is None for s in x[self.task_id]]):
                    batch_to_yield = self._transform_before_yielding(
                        x, y, batch_size)
                    yield batch_to_yield

            self.epochs_done += 1
            # one additional step is taken while logging training metrics
            self.steps_taken -= 1
        else:
            eval_batch_size = 1
            x = [[None for _ in range(eval_batch_size)]
                 for task_id in range(self.n_tasks)]
            y = [[None for _ in range(eval_batch_size)]
                 for task_id in range(self.n_tasks)]
            generators = [
                SingleTaskBatchGenerator(
                    iter_, batch_size=eval_batch_size, data_type=data_type, shuffle=shuffle)
                for iter_ in self.task_iterators.values()
            ]
            for step in range(max_task_data_len):
                for task_id in range(self.n_tasks):
                    x[task_id], y[task_id] = generators[task_id].__next__()

                yield self._transform_before_yielding(x, y, eval_batch_size)

    def get_instances(self, data_type: str = "train"):
        """
        Returns a tuple of inputs and outputs from all datasets. Lengths of
        and outputs are equal to the size of the largest dataset. Smaller
        datasets are padded with Nones until their sizes are equal to the size of the
        largest dataset.

        Args:
            data_type: can be either 'train', 'test', or 'valid'

        Returns:
            A tuple of all inputs for a data type and all expected outputs
            for a data type.
        """

        max_task_data_len = max(
            [
                len(iter_.get_instances(data_type)[0])
                for iter_ in self.task_iterators.values()
            ]
        )
        x_instances = []
        y_instances = []
        for task_name, iter_ in self.task_iterators.items():
            x, y = iter_.get_instances(data_type)
            n_repeats = math.ceil(max_task_data_len / len(x))
            x *= n_repeats
            y *= n_repeats
            x_instances.append(x[:max_task_data_len])
            y_instances.append(y[:max_task_data_len])
        error_msg = f'Len of x_instances {len(x_instances)} and y_instances {len(y_instances)} dont match'
        assert len(x_instances) == len(y_instances), error_msg
        instances = (tuple(zip(*x_instances)), tuple(zip(*y_instances)))
        return instances


class SingleTaskBatchGenerator:
    """
    Batch generator for a single task.
    If there are no elements in the dataset to form another batch, Nones are returned.

    Args:
        dataset_iterator: dataset iterator from which batches are drawn.
        batch_size: size fo the batch.
        data_type: "train", "valid", or "test"
        shuffle: whether dataset will be shuffled.
        n_batches: the number of batches that will be generated.
        size_of_the_last_batch: used if dataset size not evenly divisible by batch size.
    """

    def __init__(
            self,
            dataset_iterator: Union[DataLearningIterator],
            batch_size: int,
            data_type: str,
            shuffle: bool,
            n_batches: Optional[int] = None,
            size_of_last_batch: Optional[int] = None,
    ):
        self.dataset_iterator = dataset_iterator
        self.batch_size = batch_size
        self.data_type = data_type
        self.shuffle = shuffle
        self.n_batches = n_batches
        self.size_of_last_batch = (
            self.batch_size if size_of_last_batch is None else size_of_last_batch)

        self.inner_batch_size = math.gcd(
            len(self.dataset_iterator.data[data_type]), batch_size
        )
        self.gen = self.dataset_iterator.gen_batches(
            self.inner_batch_size, self.data_type, self.shuffle
        )
        self.batch_count = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.n_batches is not None and self.batch_count > self.n_batches:
            raise StopIteration
        x, y = (), ()
        stop_iteration = False
        while (len(x) < self.batch_size or len(y) < self.batch_size):
            try:
                xx, yy = next(self.gen)
                x += xx
                y += yy
            except StopIteration:
                x_nones = tuple([None for _ in range(self.batch_size)])
                y_nones = x_nones
                return x_nones, y_nones

        assert len(x) == self.batch_size and len(y) == self.batch_size
        self.batch_count += 1
        if self.batch_count == self.n_batches:
            x = x[: self.size_of_last_batch]
            y = y[: self.size_of_last_batch]
        return x, y
