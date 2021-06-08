# ********************************************************** #
import json
from torch.utils.data import DataLoader
import cv2
from torch.utils.data import Dataset
import os
import torch.nn as nn
import torch
import torch.nn.functional as F
import torchvision.models as models
from dropblock import DropBlock2D
import multiprocessing
import numpy as np
from torch import optim
from tqdm import tqdm
from torch.autograd import Variable
import torchvision.transforms as transforms
import math
import gc
import random
import functools
from the_network import resnet50

epoch_count = 0
acc_best = 0.
test_init = None
test_epoch = 1
output_dir = './'
rounds = 0
cv2.setNumThreads(0)
cv2.ocl.setUseOpenCL(False)


class Sharpen(object):
    def __init__(self, p=0.5):
        self.p = p

    def __call__(self, sample):
        if random.uniform(0., 1.) < self.p:
            return sample
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], np.float32)
        for i in range(len(sample['img_list'])):
            sample['img_list'][i] = cv2.filter2D(sample['img_list'][i], -1, kernel=kernel)
        return sample


class Rotation(object):
    def __init__(self, angle=5, p=0.5):
        self.angle = angle
        self.p = p

    def __call__(self, sample):
        if random.uniform(0.0, 1.0) < self.p:
            return sample
        ang_rot = np.random.uniform(self.angle) - self.angle / 2
        for i in range(len(sample['img_list'])):
            h, w, _ = sample["img_list"][i].shape
            transform = cv2.getRotationMatrix2D((w / 2, h / 2), ang_rot, 1)
            borderValue = np.mean(sample["img_list"][i][0], axis=0).astype(np.float64)
            sample["img_list"][i] = cv2.warpAffine(sample["img_list"][i], transform, (w, h),
                                                   borderValue=borderValue)
        return sample


class Translation(object):
    def __init__(self, p=0.5):
        self.p = p
        self.CONSTANT = 1e-3

    def __call__(self, sample):

        if random.uniform(0.0, 1.0) <= self.p:
            return sample
        for i in range(len(sample['img_list'])):
            h, w, _ = sample["img_list"][i].shape
            trans_range = (w / 10, h / 10)
            tr_x = trans_range[0] * random.uniform(0.0, 1.0) - trans_range[0] / 2 + self.CONSTANT
            tr_y = trans_range[1] * random.uniform(0.0, 1.0) - trans_range[1] / 2 + self.CONSTANT
            transform = np.float32([[1, 0, tr_x], [0, 1, tr_y]])
            borderValue = np.mean(sample["img_list"][i][0], axis=0).astype(np.float64)
            sample["img_list"][i] = cv2.warpAffine(sample["img_list"][i], transform, (w, h),
                                                   borderValue=borderValue)
        return sample


class Normalization(object):
    def __init__(self, mean=(0, 0, 0), std=(255, 255, 255)):
        self.mean = mean
        self.std = std

    def __call__(self, sample):
        # norm_func = transforms.Normalize(self.mean, self.std)
        for i in range(len(sample['img_list'])):
            for j in range(3):  # for colored image
                sample['img_list'][i][:, :, j] = np.array(list(map(lambda x: (x - self.mean[0]) / self.std[0],
                                                                   sample['img_list'][i][:, :, j])))
            # sample['img_list'][i] = norm_func(torch.from_numpy(sample['img_list'][i].transpose(2, 0, 1)).float())
            # sample['img_list'][i] = sample['img_list'][i].numpy().transpose(1, 2, 0)
        return sample


# sample: tuple (x:left, y:top, h:height, w:width)
class TextDatasetWithBBox(Dataset):
    def __init__(self, data_json, data_path, transform=None, isTrain=True, train_size=8000, val_size=800):
        super().__init__()
        self.data_label = list()
        self.data_bbox = list()
        for x in data_json.keys():
            data = data_json[x]
            self.data_label.append(data['label'])
            # single_image_bbox = list()
            single_image_bbox = np.zeros((len(data['label']), 4))
            for i in range(len(data['label'])):
                # single_image_bbox.append((data['left'][i], data['top'][i], data['height'][i], data['width'][i]))
                single_image_bbox[i] = (data['left'][i], data['top'][i], data['height'][i], data['width'][i])
            self.data_bbox.append(single_image_bbox)
        if isTrain:
            self.data_bbox = self.data_bbox[:train_size]
            self.data_label = self.data_label[:train_size]
            self.data_path = data_path[:train_size]
        else:
            self.data_bbox = self.data_bbox[:val_size]
            self.data_label = self.data_label[:val_size]
            self.data_path = data_path[:val_size]
        self.transform = transform

    def __len__(self):
        return len(self.data_label)

    def set_mode(self, mode=None):
        self.mode = mode

    @staticmethod
    def _Resize(bbox):  # (left=x, top=y, height=h, width=w)
        return math.floor(bbox[1]), math.ceil(bbox[1] + bbox[2]), math.floor(bbox[0]), math.ceil(bbox[0] + bbox[3])

    @staticmethod
    def _Rotate(image, top, left):  # return rotated image list
        top2 = top[1:] + [0]
        left2 = left[1:] + [0]
        delta_y = tuple(map(lambda x: x[0] - x[1], zip(top, top2)))
        delta_x = tuple(map(lambda x: x[0] - x[1], zip(left, left2)))
        if 0 in delta_y or 0 in delta_x:
            return image
        k = np.mean(list(map(lambda x: x[0] / x[1], zip(delta_y, delta_x))))
        angle = math.atan(k)
        angle = np.degrees(angle)
        for i in range(len(image)):
            h, w, _ = image[i].shape
            borderValue = np.mean(image[i][0], axis=0).astype(np.float64)
            transforms = cv2.getRotationMatrix2D((w / 2, h / 2), -angle, 1)
            image[i] = cv2.warpAffine(image[i], transforms, (w, h),
                                      borderValue=borderValue)
        return image

    def __getitem__(self, idx):
        gt_label = self.data_label[idx]  # [1,9]
        label = list()
        bbox = self.data_bbox[idx]  # [(loc1), (loc9)] needs recognition
        img = cv2.imread(self.data_path[idx])  # select idx-th image
        img_list = list()
        h, w = 0, 0
        for i, bb in enumerate(bbox):
            bb_resize = self._Resize(bb)
            if functools.reduce(lambda x, y: x * y, bb_resize) < 0:
                continue
            resized_img = img[bb_resize[0]:bb_resize[1], bb_resize[2]:bb_resize[3]].astype(np.float32)
            h, w = max(h, bb_resize[1] - bb_resize[0]), max(w, bb_resize[3] - bb_resize[2])
            img_list.append(resized_img)
            label.append(gt_label[i])

        if len(img_list) > 1:
            self._Rotate(img_list, bbox[:, 1], bbox[:, 0])
        # for recognition of each number
        is_end = [0] * (len(img_list) - 1) + [1] if len(img_list) else []
        sample = {'img_list': img_list, 'label': label, 'largest_size': (h, w),
                  'is_end': is_end}
        if self.transform:
            sample = self.transform(sample)
        return sample


def pred_2_number(preds, cuda=True):
    preds = nn.Softmax(dim=1)(preds)
    res = list()
    for i in range(len(preds)):
        if cuda:
            res.append(preds[i].argmax().cpu())
        else:
            res.append(preds[i].argmax())
    return torch.Tensor(res).int()


class Mish(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x * torch.tanh(F.softplus(x))


# # mata-ACON
# class meta_ACON(nn.Module):
#     def __init__(self, p1=1, p2=0, mode=None):
#         super().__init__()
#         self.p1 = p1
#         self.p2 = p2
#         self.mode = mode
#
#     def _cal_beta(self, input):  # (BS, C, H, W)
#         if self.mode == 'pixel_wise':
#             beta = nn.Sigmoid()(input)
#         elif self.mode == 'channel_wise':
#             beta = nn.Sigmoid()(torch.sum(input, dim=0))
#         elif self.mode == 'layer_wise':
#             beta = nn.Sigmoid()(torch.sum(input, dim=(0, 1)))
#         elif self.mode is None:
#             beta = 1.
#         else:
#             return NotImplementedError('Invalid mode.')
#         return beta
#
#     def forward(self, input):
#         output = (self.p1 - self.p2) * input * nn.Sigmoid()(
#             self._cal_beta(input) * (self.p1 - self.p2) * input) + self.p2 * input
#         return output


class Resnet50Mod(nn.Module):
    def __init__(self, num_class=11):
        super(Resnet50Mod, self).__init__()
        self.cnn = nn.Sequential(*list(origin_net.children())[:-1])
        self.hidden_layer = nn.Linear(2048, 128)
        self.dropout = DropBlock2D(block_size=3, drop_prob=0.2)
        self.output = nn.Linear(128, num_class)  # 11 or 10, 1 * 11

    def forward(self, img):  # img: bs, c, h, w
        img = self.cnn(img).view(img.size(0), -1)
        assert img.size(1) == 2048
        img = self.hidden_layer(img)
        img = self.dropout(img.view(-1, img.size(1), 1, 1))
        return self.output(img.view(img.size(0), -1))

    @staticmethod
    def pred_2_number(preds):
        preds = nn.Softmax(dim=1)(preds)
        res = list()
        for i in range(len(preds)):
            res.append(preds[i].argmax())
        return torch.Tensor(res).int()

    # def Mish(self, x):
    #     return x * torch.tanh(F.softplus(x))


class LabelSmoothing(nn.Module):
    """ NLL loss with label smoothing. """

    def __init__(self, smoothing=0.0):
        """ Constructor for the LabelSmoothing module.
        :param smoothing: label smoothing factor """
        super(LabelSmoothing, self).__init__()
        self.confidence = 1.0 - smoothing
        self.smoothing = smoothing

    def forward(self, x, target):  # x 到底是个啥样的输入：raw output with no probabilities?
        logprobs = torch.nn.functional.log_softmax(x, dim=-1)
        target = target.long()
        nll_loss = -logprobs.gather(dim=-1, index=target.unsqueeze(1))
        nll_loss = nll_loss.squeeze(1)
        smooth_loss = -logprobs.mean(dim=-1)
        loss = self.confidence * nll_loss + self.smoothing * smooth_loss
        return loss.mean()


# step learning rate
class StepLR(object):
    def __init__(self, optimizer, step_size=1000, max_iter=10000):
        self.optimizer = optimizer
        self.max_iter = max_iter
        self.step_size = step_size
        self.last_iter = -1
        self.base_lrs = list(map(lambda group: group['lr'], optimizer.param_groups))

    def get_lr(self):
        return self.optimizer.param_groups[0]['lr']

    def step(self, last_iter=None):
        if last_iter is not None:
            self.last_iter = last_iter
        if self.last_iter + 1 == self.max_iter:
            self.last_iter = -1
        self.last_iter = (self.last_iter + 1) % self.max_iter
        for ids, param_group in enumerate(self.optimizer.param_groups):
            param_group['lr'] = self.base_lrs[ids] * 0.8 ** (self.last_iter // self.step_size)


class CosineLR(optim.lr_scheduler.CosineAnnealingLR):
    def __init__(self, optimizer, T_max, eta_min=0, last_epoch=-1, verbose=False):
        super(CosineLR, self).__init__(optimizer, T_max, eta_min, last_epoch, verbose)
        self.last_iter = -1

    def my_step(self):
        self.last_iter += 1
        super().step()

    # def get_lr(self):
    #     super().get_lr()


def load_network(base_lr=1e-3, cuda=True):
    network = Resnet50Mod()
    if cuda:
        network = network.cuda()
    # optimizer = optim.Adam(network.parameters(), lr=base_lr, weight_decay=0.0001)
    # lr_scheduler = StepLR(optimizer, step_size=step_size, max_iter=max_iter)

    optimizer = optim.SGD(network.parameters(), lr=base_lr, momentum=0.9, weight_decay=0.001)
    # lr_scheduler = optim.lr_scheduler.CosineAnnealingLR(my_optimizer, len(train_dataset)//batch_size)
    lr_scheduler = CosineLR(optimizer, len(train_dataset) // batch_size)

    loss_function = LabelSmoothing(0.2)
    return network, optimizer, lr_scheduler, loss_function


def test(network, dataset, cuda=True):
    count = 0
    tp = 0
    val_dl = DataLoader(dataset, batch_size=batch_size, shuffle=False,
                        collate_fn=text_collate)
    iterator = tqdm(val_dl)
    for sample in iterator:
        imgs = sample["img_list"]  # (bs, num_img, c, h, w)
        imgs = imgs.view(imgs.shape[0] * imgs.shape[1], imgs.shape[2], imgs.shape[3], imgs.shape[4])
        true_label = sample['label'].view(-1)
        segments = sample['is_end'].view(-1)
        img = Variable(imgs)
        if cuda:
            img = img.cuda()
        out = network(img)
        out = pred_2_number(out, cuda)
        single_out = list()
        single_gt = list()
        for i in segments:
            if segments[i] == 1:  # end
                single_out.append(out[i])
                single_gt.append(true_label[i])
                if single_out == single_out:
                    tp += 1
                count += 1
                single_out.clear()
                single_gt.clear()
            elif segments[i] == 0:
                single_out.append(out[i])
                single_gt.append(true_label[i])
            else:
                continue
        status = "acc: {0:.4f}".format(tp / count)
        iterator.set_description(status)

    return tp / count


# ***************************************************************** #

def text_collate(batch):
    imgs = list()
    labels = list()
    isEnd = list()
    h, w = 0, 0
    seq_len = 6
    # find size to be padded
    for sample in batch:
        h, w = int(max(h, sample['largest_size'][0])), int(max(w, sample['largest_size'][1]))

    color = (255, 255, 255)
    all_white = np.zeros((3, h, w))

    for sample in batch:
        img = list()
        for origin_img in sample['img_list']:
            padding_height = h - origin_img.shape[0]
            top = padding_height >> 1
            bottom = padding_height - top
            padding_width = w - origin_img.shape[1]
            left = padding_width >> 1
            right = padding_width - left
            origin_img = cv2.copyMakeBorder(origin_img.copy(), top, bottom, left, right, cv2.BORDER_CONSTANT,
                                            value=color)
            img.append(origin_img.transpose((2, 0, 1)))
        if len(sample['img_list']) < seq_len:
            remain = seq_len - len(sample['img_list'])
            labels.append(sample['label'] + [10] * remain)
            isEnd.append(sample['is_end'] + [-1] * remain)
            while remain:
                img.append(all_white)
                remain -= 1
        else:
            labels.append(sample['label'])
            isEnd.append(sample['is_end'])
        img = torch.Tensor(img)
        imgs.append(img)

    imgs = torch.stack(imgs)  # each tensor with equal size
    labels = torch.Tensor(labels).int()
    isEnd = torch.Tensor(isEnd)
    batch = {"img_list": imgs, "label": labels, 'is_end': isEnd}
    return batch


def WithCuda():
    gpu_num = torch.cuda.device_count()
    os.environ['CUDA_VISIBLE_DEVICES'] = '0' if gpu_num > 0 else ''
    return gpu_num > 0


if __name__ == '__main__':
    train_json = json.load(open('mchar_train.json'))
    train_path = ['mchar_train/' + x for x in train_json.keys()]

    val_json = json.load(open('mchar_val.json'))
    val_path = ['mchar_val/' + x for x in val_json.keys()]

    print("#********************************************# Loading Raw Data Completed!")

    origin_net = resnet50(pretrained=True)
    # origin_net.relu = meta_ACON(mode='layer_wise')
    # origin_net.avgpool = nn.AdaptiveAvgPool2d(1)

    transform = transforms.Compose(
        [
            Sharpen(),
            Rotation(),
            Translation(),
            Normalization()
        ]
    )
    train_dataset = TextDatasetWithBBox(train_json, train_path, transform=transform, isTrain=True)
    val_dataset = TextDatasetWithBBox(val_json, val_path, transform=transform, isTrain=False)

    cuda = True if WithCuda() else False
    num_workers = multiprocessing.cpu_count()
    batch_size = 10

    print("#********************************************# Building Dataset Completed!")

    network, optimizer, lr_scheduler, loss_function = load_network(cuda=cuda)
    while True:
        print('#********************************************# Activating Training Process!')
        if (test_epoch is not None and epoch_count != 0 and epoch_count % test_epoch == 0) or (
                test_init and epoch_count == 0):
            print("Test phase")
            train_dataset.set_mode("test")
            network = network.eval()
            acc = test(network, val_dataset, cuda)
            network = network.train()
            train_dataset.set_mode("train")
            if acc > acc_best:
                rounds = 0
                if output_dir is not None:
                    torch.save(network.state_dict(), os.path.join(output_dir + "resnet50_best"))
                acc_best = acc
            else:
                rounds += 1
                if rounds > 4:
                    print('Test Accuracy does not improve in the past five epochs!\n')
                    print("acc: {}\tacc_best: {};".format(acc, acc_best))
                    break
            print("acc: {}\tacc_best: {};".format(acc, acc_best))

        loss_mean = list()
        train_dl = DataLoader(train_dataset, batch_size=batch_size, num_workers=num_workers, shuffle=True,
                              collate_fn=text_collate)
        iterator = tqdm(train_dl)
        for sample in iterator:
            img_list = sample['img_list']
            img_list = img_list.view(img_list.shape[0] * img_list.shape[1], img_list.shape[2], img_list.shape[3],
                                     img_list.shape[4])
            if cuda:
                network = network.cuda()
                img_list = img_list.cuda()
                sample['label'] = sample['label'].cuda()
            optimizer.zero_grad()
            pred = network(img_list)
            loss = loss_function(pred, Variable(sample['label']).view(-1))
            # loss_function = nn.CrossEntropyLoss()
            # loss = loss_function(pred, Variable(sample['label']).view(-1).long())
            loss.backward()
            nn.utils.clip_grad_norm_(network.parameters(), 10.0)
            loss_mean.append(loss.item())
            status = "epoch: {}; iter: {}; lr: {}; loss_mean: {}; loss: {}".format(epoch_count,
                                                                                   lr_scheduler.last_iter,
                                                                                   lr_scheduler.get_lr()[0],
                                                                                   np.mean(loss_mean), loss.item())
            iterator.set_description(status)
            optimizer.step()
            lr_scheduler.my_step()
        if output_dir is not None:
            torch.save(network.state_dict(), os.path.join(output_dir + "resnet50_last"))
        if epoch_count > 20:
            break
        epoch_count += 1
        lr_scheduler = CosineLR(optimizer, len(train_dl))
        if cuda:
            gc.collect()
            torch.cuda.empty_cache()

