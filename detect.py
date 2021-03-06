from __future__ import division
import time
import torch
import torch.nn as nn
from torch.autograd import Variable
import numpy as np
import cv2
from util import *
import argparse
import os
import os.path as osp
from darknet import Darknet
import pickle as pkl
import pandas as pd
import colorsys
import random


def arg_parse():
    """
    Parse arguments to the detect module
    """

    parser = argparse.ArgumentParser(description='YOLO v3 Detection Module')

    parser.add_argument("--images", dest='images', 
            help="Image / Directory containing images to perform detection upon",
            default = "imgs", type=str)
    parser.add_argument("--det", dest='det', 
            help="Image / Directory to store detections to",
            default="det", type=str)
    parser.add_argument("--bs", dest="bs", help="Batch size", default=1, type=int)
    parser.add_argument("--confidence", dest="confidence", help="Object Confidence to filter predictions", default=0.5, type=float)
    parser.add_argument("--nms_thresh", dest="nms_thresh", help="NMS Threshold", default=0.4, type=float)
    parser.add_argument("--cfg", dest="cfgfile", help="Config file", default="cfg/yolov3.cfg", type=str)
    parser.add_argument("--weights", dest="weightsfile", help="weightsfile", default="yolov3.weights", type=str)
    parser.add_argument("--reso", dest="reso", help="Input resolution of the network. Increase to increase accuracy. Descrease to increase speed", default=416, type=float)

    return parser.parse_args()

if __name__ == '__main__':
    args = arg_parse()
    images = args.images
    batch_size = args.bs
    confidence = args.confidence
    nms_thresh = args.nms_thresh
    start = 0
    CUDA = torch.cuda.is_available()
    classes = load_classes("data/coco.names")
    num_classes = len(classes)

    # setup the neural network
    print("Loading network...")
    model = Darknet(args.cfgfile)
    model.load_weights(args.weightsfile)
    print("Network successfully loaded!!")

    model.net_info["height"] = args.reso
    inp_dim = int(model.net_info["height"])
    assert inp_dim % 32 == 0 
    assert inp_dim > 32

    # if there's a GPU available, put the model on GPU
    if CUDA:
        model.cuda()

    # set the model in evaluation(prediction) mode
    model.eval()

    read_dir = time.time() # check point time
    # detection phase
    try:
        imlist = [os.path.join(os.path.realpath('.'), images, img) for img in os.listdir(images)]
    except NotADirectoryError:
        imlist = []
        imlist.append(os.path.join(os.path.realpath('.'), images))
    except FileNotFoundError:
        print("No file or directory with the name {}".format(images))
        exit()
    
    # if detection result path is not found, create it
    if not os.path.exists(args.det):
       os.makedirs(args.det)

    load_batch = time.time() # check point time

    loaded_images = [cv2.imread(x) for x in imlist]

    # pytorch variables for images
    # im_batches is input to model
    im_batches = list(map(prep_image, loaded_images, [inp_dim for _ in range(len(imlist))] ))

    # list containing dimensions of original images
    img_dim_list = [(loaded_image.shape[1], loaded_image.shape[0]) for loaded_image in loaded_images] 
    #NOTE why repeat?
    #img_dim_list = torch.FloatTensor(img_dim_list).repeat(1, 2)
    img_dim_list = torch.FloatTensor(img_dim_list)

    if CUDA:
        img_dim_list = img_dim_list.cuda()

    leftover = 0
    if len(img_dim_list) % batch_size:
        leftover = 1

    if batch_size != 1:
        num_batches = len(imlist) // batch_size + leftover
        im_batches = [torch.cat((im_batches[i * batch_size : min( (i+1) * batch_size, len(im_batches) )]))
                for i in range(num_batches)] # only last batch num is result of batch_size % batch_size

    write = False
    start_det_loop = time.time()

    for i, batch in enumerate(im_batches):
        # load the image
        start = time.time()
        if CUDA:
            batch = batch.cuda()

        # apply offsets to the result predictions
        # transform the predictions as described in the YOLO paper
        # flatten the prediction vector
        # B x (bbox cord x No. of anchors) x grid_w x grid_h -> B x bbox x (all the boxes)
        # put every proposed box as a row

        with torch.no_grad():
            prediction = model(batch, CUDA)

            prediction = write_results(prediction, confidence, num_classes, nms_conf=nms_thresh)

            if prediction == None:
                continue

            end = time.time()

            batch_start = i * batch_size
            batch_end = min((i + 1) * batch_size, len(imlist))

            prediction[:, 0] += i * batch_size # transform the attribute from index in batch to index in imlist


            for im_num, image in enumerate(imlist[batch_start:batch_end]):
                img_idx = batch_start + im_num
                objs = [classes[int(x[-1])] for x in prediction if int(x[0] == img_idx)] 
                print("{0:20} predicted in {1:6.3f} seconds".format(image.split("/")[-1], (end - start)/batch_size))
                print("{0:20} {1:s}".format("Objects Detected:", " ".join(objs)))
                print("---------------------------------------------------------------")

            if CUDA:
                torch.cuda.synchronize()

            if not write:
                outputs = prediction
                write = True
            else:
                outputs = torch.cat((outputs, prediction))

    # draw BBox to images
    try:
        outputs
    except NameError:
        print("No detections were made")
        exit()

    # prepare img_dim for each output
    """
    Return [
        [img_w, img_h],
        [img_w, img_h],,,
    ]
    """
    img_dim_list = torch.index_select(img_dim_list, 0, outputs[:,0].long())
    
    # inp_dim: img dim to input to network
    # torch.min returns value with selected value index, so get only[0], and reshape 
    scaling_factor = torch.min(inp_dim/img_dim_list, 1)[0].view(-1, 1)

    outputs[:, [1,3]] -= (inp_dim - scaling_factor * img_dim_list[:, 0].view(-1, 1)) / 2
    outputs[:, [2,4]] -= (inp_dim - scaling_factor * img_dim_list[:, 1].view(-1, 1)) / 2

    outputs[:, 1:5] /= scaling_factor

    # output range to (0, [w, h])
    for i in range(outputs.shape[0]):
        outputs[i, [1, 3]] = torch.clamp(outputs[i, [1,3]], 0.0, img_dim_list[i, 0])
        outputs[i, [2, 4]] = torch.clamp(outputs[i, [2,4]], 0.0, img_dim_list[i, 1])

    output_recast = time.time()

    class_load = time.time()

    hsv_tuples = [(x / num_classes, 1., 1.) for x in range(num_classes)]
    colors = list(map(lambda x: colorsys.hsv_to_rgb(*x), hsv_tuples))
    colors = list(map(lambda x: (int(x[0] * 200), int(x[1] * 200), int(x[2] * 200)), colors))
    np.random.seed(10000)
    np.random.shuffle(colors)
    np.random.seed(None) # reset seed to default.

    draw = time.time()

    def write(x, img, color):
        c1 = tuple(x[1:3].int())
        c2 = tuple(x[3:5].int())
        cls = int(x[-1])
        label = "{0}".format(classes[cls])
        cv2.rectangle(img, c1, c2, color, 4) # draw rectangle
        t_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_PLAIN, 1, 1)[0]
        c2 = c1[0] + t_size[0] + 3, c1[1] + t_size[1] + 4
        cv2.rectangle(img, c1, c2, color, -1)
        cv2.putText(img, label, (c1[0], c1[1] + t_size[1] + 1), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), thickness=1)
 
    for output in outputs:
        img = loaded_images[int(output[0])]
        color = colors[int(output[-1])]
        write(output, img, color)

    det_names = pd.Series(imlist).apply(lambda x: "{}/det_{}".format(args.det, x.split("/")[-1]))

    for (det_name, img) in zip(det_names, loaded_images):
        cv2.imwrite(det_name, img)
    #list(map(cv2.imwrite, det_names, loaded_images))

    end = time.time()

    print("SUMMARY")
    print("---------------------------------------------------------------")
    print("{:25}".format("Task", "Time Taken (in seconds)"))
    print()
    print("{:25}: {:2.3f}".format("Reading addresses", load_batch - read_dir))
    print("{:25}: {:2.3f}".format("Loading batch", start_det_loop - load_batch))
    print("{:25}: {:2.3f}".format("Detection (" + str(len(imlist)) + " images)", output_recast - start_det_loop))
    print("{:25}: {:2.3f}".format("Output Processing", class_load - output_recast))
    print("{:25}: {:2.3f}".format("Drawing Boxes", end - draw))
    print("{:25}: {:2.3f}".format("Average time_per_img", (end - load_batch)/len(imlist)))

    torch.cuda.empty_cache()



