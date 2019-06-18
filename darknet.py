from __future__ import division

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
from util import *

def get_test_input():
    img = cv2.imread("dog-cycle-car.png")
    img = cv2.resize(img, (416, 416))
    img = img[:,:,::-1].transpose((2,0,1)) # BGR->RGB | HXWC -> CXHXW
    img = img[np.newaxis, :, :, :]/255.0 # add a channel at 0 (for batch) | Normalize
    img = torch.from_numpy(img).float() # convert float torch tensor
    img = Variable(img) # convert to Variable
    return img


def parse_cfg(cfgfile):
    """
    Takes a configuration file
    
    Returns a list of blocks. Each blocks describes a block in the neural
    network to be built. Block is represented as a dictionary in the list

    """

    block = {}
    blocks = []
    with open(cfgfile, 'r') as f:
        lines = f.read().split('\n')
        lines = [x.rstrip().lstrip() for x in lines if len(x) > 0 and x[0] != '#']
        
        for line in lines:
            if line[0] == "[": # This marks the start of a new block
                if len(block) != 0: # If block is not empty, implies it is storing values of previous layer.
                    blocks.append(block) # add it the blocks list
                    block = {} # and re-init the block
                block["type"] = line [1:-1].rstrip() # split '[' and ']'
            else: 
                key, value = [ x.strip() for x in line.split("=") ]
                block[key] = value 
        blocks.append(block) # append last section

    return blocks

class EmptyLayer(nn.Module):
    def __init__(self):
        super(EmptyLayer, self).__init__()

class DetectionLayer(nn.Module):
    def __init__(self, anchors):
        super(DetectionLayer, self).__init__()
        self.anchors = anchors

    def forward(self, x, inp_dim, num_classes, confidence):
        x = x.data
        global CUDA
        prediction = x
        prediction = predict_transform(prediction, inp_dim, self.anchors, num_classes, confidence, CUDA)
        return prediction

class Upsample(nn.Module):        
    def __init__(self, stride=2):
        super(Upsample, self).__init__()
        self.stride = stride

    def forward(self, x):
        stride = self.stride
        assert(x.data.dim() == 4)
        B = x.data.size(0)
        C = x.data.size(1)
        H = x.data.size(2)
        W = x.data.size(3)
        ws = stride
        hs = stride
        x = x.view(B, C, H, 1, W, 1).expand(B, C, H, stride, W, stride).contiguous().view(B, C, H*stride, W*stride)
        return x

class Darknet(nn.Module):
    def __init__(self, cfgfile):
        super(Darknet, self).__init__()
        self.blocks = parse_cfg(cfgfile)
        self.net_info, self.module_list = create_modules(self.blocks)
        #self.header = torch.IntTensor([0, 0, 0, 0])
        #self.seen = 0

    def forward(self, x, CUDA):
        modules = self.blocks[1:]
        outputs = {} # we cache the outputs for the route layer
        # key: layer index, value: output feature map

        # predict_transform cannot concatenate empty tensor
        # so, write is False first feature map have not been output 
        write = False

        for i in range(len(modules)):
            
            module_type = (modules[i]["type"])
            if module_type in ("convolutional", "upsample", "maxpool"):
                print(f"module_type: {module_type}")
                x = self.module_list[i](x)
                outputs[i] = x

            elif module_type == "route":
                layers = [int(a) for a in modules[i]["layers"]]
                
                # NOTICE this calculate is needed?
                if layers[0] > 0:
                    layers[0] = layers[0] - i

                if len(layers) == 1:
                    x = outputs[i + layers[0]]

                else:
                    if layers[1] > 0:
                        layers[1] = layers[1] - i

                    map1 = outputs[i + layers[0]]
                    map2 = outputs[i + layers[1]]

                    x = torch.cat((map1, map2), 1)
                outputs[i] = x

            elif module_type == "shortcut":
                from_ = int(modules[i]["from"])
                x = outputs[i-1] + outputs[i+from_]
                outputs[i] = x

            elif module_type == 'yolo':
                anchors = self.module_list[i][0].anchors
                # get the input dimensions
                inp_dim = int(self.net_info["height"])

                # get the number of classes
                num_classes = int(modules[i]["classes"])

                # output the result
                x = x.data
                x = predict_transform(x, inp_dim, anchors, num_classes, CUDA)

                if type(x) == int:
                    continue
                
                if not write:
                    detections = x
                    write = True
                else:
                    detections = torch.cat((detections, x), 1)

                outputs[i] = outputs[i-1]

        try:
            return detections
        except:
            return 0



                    

        



def create_modules(blocks):
    net_info = blocks[0] # capture the information about the input and pre-processing
    module_list = nn.ModuleList()
    index = 0 # indexing blocks helps with implementing route layers(skip connections)
    prev_filters = 3
    output_filters = []

    for x in blocks:
        module = nn.Sequential()  

        if x["type"] == "net":
            continue

        # if it's convolutional layer
        if x["type"] == "convolutional":
            # get the info about the layer
            activation = x["activation"]
            try:
                batch_normalize = int(x["batch_normalize"])
                bias = False
            except:
                batch_normalize = 0
                bias = True

            filters = int(x["filters"])
            padding = int(x["pad"])
            kernel_size = int(x["size"])
            stride = int(x["stride"])

            if padding:
                pad = (kernel_size - 1) // 2
            else:
                pad = 0
            # add the convolutional layer
            conv = nn.Conv2d(prev_filters, filters, kernel_size, stride, pad, bias=bias)
            module.add_module("conv_{0}".format(index), conv)

            # add the batch norm layer
            if batch_normalize:
                bn = nn.BatchNorm2d(filters)
                module.add_module("batch_norm_{0}".format(index), bn)

            # check the activation
            # it is either linear or leaky ReLU for YOLO
            if activation == "leaky":
                activn = nn.LeakyReLU(0.1, inplace=True)
                module.add_module("leaky_{0}".format(index), activn)

        # if it's an upsampling layer
        # we use Bilinear2dUpsampling
        elif x["type"] == "upsample":
            stride = int(x["stride"])
            upsample = nn.Upsample(scale_factor=2, mode="nearest")
            module.add_module("leaky_{0}".format(index), upsample)
        
        # if it's route layer
        elif x["type"] == "route":
            x["layers"] = x["layers"].split(',') 

            # start of a route
            start = int(x["layers"][0])

            # end, if there exists one.
            try:
                end = int(x["layers"][1])
            except:
                end = 0

            # positive anotation
            if start > 0:
                start = start - index
            if end > 0:
                end = end - index

            route = EmptyLayer()
            module.add_module("route_{0}".format(index), route)

            if end < 0:
                filters = output_filters[index + start] + output_filters[index + end]
            else:
                filters = output_filters[index + start]

            # shortcut corresponds to skip connection
        elif x["type"] == "shortcut":
                from_ = int(x["from"])
                shortcut = EmptyLayer()
                module.add_module("shortcut_{}".format(index), shortcut)

        elif x["type"] == "maxpool":
            stride = int(x["stride"])
            size = int(x["size"])
            if stride != 1:
                maxpool = nn.MaxPool2d(size, stride)
            else:
                maxpool = MaxPoolStride1(size)
            
            module.add_module("maxpool_{}".format(index), maxpool)

        # YOLO is the detection layer
        elif x["type"] == "yolo":
            mask = [ int(x) for x in x["mask"].split(",")]

            anchors = [int(a) for a in x["anchors"].split(",")]
            anchors = [(anchors[i], anchors[i+1]) for i in range(0, len(anchors), 2)]
            anchors = [anchors[i] for i in mask]

            detection = DetectionLayer(anchors)
            module.add_module("Detection_{}".format(index), detection)

        else:
            print(f'TYPE: {x["type"]}')
            print("Something I dunno")
            assert False

        module_list.append(module)
        prev_filters = filters
        output_filters.append(filters)
        index += 1

    return (net_info, module_list)



    
