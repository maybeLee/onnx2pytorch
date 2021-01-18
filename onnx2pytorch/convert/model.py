from functools import partial
import warnings

import onnx
import torch
from onnx import numpy_helper
from torch import nn
from torch.jit import TracerWarning
from torch.nn.modules.conv import _ConvNd
from torch.nn.modules.batchnorm import _BatchNorm
from torch.nn.modules.linear import Identity

from onnx2pytorch.operations import Split
from onnx2pytorch.convert.operations import convert_operations


class InitParameters(dict):
    """Use for parameters that are hidden."""

    def __getitem__(self, item):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", TracerWarning)
            return torch.from_numpy(numpy_helper.to_array(super().__getitem__(item)))

    def get(self, item, default):
        if item in self:
            return self[item]
        else:
            return default


class ConvertModel(nn.Module):
    def __init__(self, onnx_model: onnx.ModelProto, batch_dim=0):
        super().__init__()
        self.onnx_model = onnx_model
        self.batch_dim = batch_dim
        self.mapping = {}
        for op_id, op_name, op in convert_operations(onnx_model, batch_dim):
            setattr(self, op_name, op)
            self.mapping[op_id] = op_name

        self.init_parameters = InitParameters(
            {tensor.name: tensor for tensor in self.onnx_model.graph.initializer}
        )
        # set output ids
        # self.output_ids = set(node.output[0] for node in onnx_model.graph.node)

    def forward(self, *input):
        if input[0].shape[self.batch_dim] > 1:
            raise NotImplementedError(
                "Input with larger batch size than 1 not supported yet."
            )
        # TODO figure out how to store only necessary activations.
        input_names = [x.name for x in self.onnx_model.graph.input]
        activations = dict(zip(input_names, input))

        for node in self.onnx_model.graph.node:
            # Identifying the layer ids and names
            out_op_id = node.output[0]
            out_op_name = self.mapping[out_op_id]
            in_op_names = [
                self.mapping.get(in_op_id, in_op_id)
                for in_op_id in node.input
                if in_op_id in activations
            ]

            # getting correct layer
            op = getattr(self, out_op_name)

            # if first layer choose input as in_activations
            # if not in_op_names and len(node.input) == 1:
            #    in_activations = input
            if isinstance(op, (nn.Linear, _ConvNd, _BatchNorm)):
                in_activations = [
                    activations[in_op_id]
                    for in_op_id in node.input
                    if in_op_id in activations
                ]
            else:
                in_activations = [
                    activations[in_op_id] if in_op_id in activations
                    # if in_op_id not in activations neither in parameters then
                    # it must be the initial input
                    # TODO loading parameters in forward func might be very slow!
                    else self.init_parameters.get(in_op_id, input[0])
                    for in_op_id in node.input
                ]

            # store activations for next layer
            if isinstance(op, partial) and op.func == torch.cat:
                activations[out_op_id] = op(in_activations)
            elif isinstance(op, Split):
                for out_op_id, output in zip(node.output, op(*in_activations)):
                    activations[out_op_id] = output
            elif isinstance(op, Identity):
                # After batch norm fusion the batch norm parameters
                # were all passed to identity instead of first one only
                activations[out_op_id] = op(in_activations[0])
            else:
                activations[out_op_id] = op(*in_activations)

        # collect all outputs
        outputs = [activations[x.name] for x in self.onnx_model.graph.output]
        if len(outputs) == 1:
            outputs = outputs[0]
        return outputs