#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import os
import types
from typing import List

import torch
from torch import nn

from . import privacy_analysis as tf_privacy
from .dp_model_inspector import DPModelInspector
from .per_sample_gradient_clip import (
    PerSampleGradientClipper,
    __clip_value_calculation_params__ as clipping_method)


class PrivacyEngine:
    def __init__(
        self,
        module: nn.Module,
        batch_size: int,
        sample_size: int,
        alphas: List[float],
        noise_multiplier: float,
        max_grad_norm: float,
        grad_norm_type: int = 2,
        batch_dim: int = 0,
        target_delta: float = 0.000001,
    ):
        self.steps = 0
        self.module = module
        self.alphas = alphas
        self.device = next(module.parameters()).device

        self.sample_rate = batch_size / sample_size
        self.noise_multiplier = noise_multiplier
        self.max_grad_norm = max_grad_norm
        self.grad_norm_type = grad_norm_type
        self.batch_dim = batch_dim
        self.target_delta = target_delta

        self.secure_seed = int.from_bytes(os.urandom(8), byteorder="big", signed=True)
        self.secure_generator = (
            torch.random.manual_seed(self.secure_seed)
            if self.device.type == "cpu"
            else torch.cuda.manual_seed(self.secure_seed)
        )
        self.validator = DPModelInspector()
        self.clipper = None  # lazy initialization in attach

    def detach(self):
        optim = self.optimizer
        optim.privacy_engine = None
        self.clipper.close()
        optim.step = types.MethodType(optim.original_step, optim)

    def attach(self, optimizer: torch.optim.Optimizer):
        """
        Attaches to a `torch.optim.Optimizer` object, and injects itself into
        the optimizer's step.

        To do that, this method does the following:
        1. Validates the model for containing un-attachable layers
        2. Adds a pointer to this object (the PrivacyEngine) inside the optimizer
        3. Moves the original optimizer's `step()` function to `original_step()`
        4. Monkeypatches the optimizer's `step()` function to call `step()` on
        the query engine automatically whenever it would call `step()` for itself
        """

        # Validate the model for not containing un-supported modules.
        self.validator.validate(self.module)
        # only attach if model is validated
        if clipping_method['method'].lower() != 'none':
            print('Warning! Current implementations of dynamic clipping '
                  'are not privacy safe; Caclulated privacy loss is not '
                  'indicative of a proper bound.')
        self.clipper = PerSampleGradientClipper(
            self.module, self.max_grad_norm, self.batch_dim
        )

        def dp_step(self, closure=None):
            self.privacy_engine.step()
            self.original_step(closure)

        optimizer.privacy_engine = self
        optimizer.original_step = optimizer.step
        optimizer.step = types.MethodType(dp_step, optimizer)

        self.optimizer = optimizer  # create a cross reference for detaching

    def get_renyi_divergence(self):
        rdp = torch.tensor(
            tf_privacy.compute_rdp(
                self.sample_rate, self.noise_multiplier, 1, self.alphas
            )
        )
        return rdp

    def get_privacy_spent(self, target_delta: float = None):
        if target_delta is None:
            target_delta = self.target_delta
        rdp = self.get_renyi_divergence() * self.steps
        return tf_privacy.get_privacy_spent(self.alphas, rdp, target_delta)

    def step(self):
        self.steps += 1
        max_norm = self.clipper.step()
        for p in self.module.parameters():
            if p.requires_grad and self.noise_multiplier > 0:
                noise = torch.normal(
                    0,
                    self.noise_multiplier * max_norm,
                    p.grad.shape,
                    device=self.device,
                    generator=self.secure_generator,
                )
                p.grad += noise / self.clipper.batch_size

    def to(self, device):
        self.device = device
        return self
