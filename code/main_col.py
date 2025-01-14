# System libs
import os
import random
import time

# Numerical libs
import torch
import torch.nn.functional as F
import torch.nn as nn
import numpy as np
import scipy.io.wavfile as wavfile
# from scipy.misc import imsave
from imageio import imwrite as imsave
from mir_eval.separation import bss_eval_sources

# Our libs
from arguments import ArgParser
from dataset import MUSICMixDataset
from models import ModelBuilder, activate
from utils import AverageMeter, \
    recover_rgb, magnitude2heatmap, \
    istft_reconstruction, warpgrid, \
    combine_video_audio, save_video, makedirs
from viz import plot_loss_metrics, HTMLVisualizer
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

class LBSign(torch.autograd.Function):

    @staticmethod
    def forward(ctx, input):
        return torch.round(input)

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output

class BinaryLoss(nn.Module):
    def __init__(self):
        super(BinaryLoss, self).__init__()

    def forward(self, m):

        return torch.mean(1.0/(1e-6 + torch.abs(m-0.5)))

def tf_data(x, B, s1, s2):
    return torch.softmax(x, dim=-1)[:, 0].view(B, 1).unsqueeze(-1).unsqueeze(-1).repeat(1, 1, s1, s2)

# Network wrapper, defines forward pass
class NetWrapper(torch.nn.Module):
    def __init__(self, nets, crit):
        super(NetWrapper, self).__init__()
        self.net_sound_ground, self.net_frame_ground, \
        self.net_sound, self.net_frame, self.net_synthesizer, self.net_grounding = nets
        self.maxpool = torch.nn.AdaptiveMaxPool1d(1)
        self.crit = crit
        self.cts = nn.CrossEntropyLoss()
        self.bn = LBSign.apply
        self.cts_bn = BinaryLoss()

    def forward(self, batch_data, args):
        mag_mix = batch_data['mag_mix']
        mags = batch_data['mags']
        frames = batch_data['frames']
        mag_mix = mag_mix + 1e-10

        N = args.num_mix
        B = mag_mix.size(0)
        T = mag_mix.size(3)

        # 0.0 warp the spectrogram
        if args.log_freq:
            grid_warp = torch.from_numpy(
                warpgrid(B, 256, T, warp=True)).to(args.device)
            mag_mix = F.grid_sample(mag_mix, grid_warp)
            for n in range(N):
                mags[n] = F.grid_sample(mags[n], grid_warp)

        # 0.1 calculate loss weighting coefficient: magnitude of input mixture
        if args.weighted_loss:
            weight = torch.log1p(mag_mix)
            weight = torch.clamp(weight, 1e-3, 10)
        else:
            weight = torch.ones_like(mag_mix)

        # 0.2 ground truth masks are computed after warpping!
        gt_masks = [None for n in range(N)]
        for n in range(N):
            if args.binary_mask:
                # for simplicity, mag_N > 0.5 * mag_mix
                gt_masks[n] = (mags[n] > 0.5 * mag_mix).float()
            else:
                gt_masks[n] = mags[n] / mag_mix
                # clamp to avoid large numbers in ratio masks
                gt_masks[n].clamp_(0., 5.)

        gt_masks[1] = torch.mul(gt_masks[3], 0.)
        gt_masks[3] = torch.mul(gt_masks[3], 0.)
        # LOG magnitude
        log_mag_mix = torch.log1p(mag_mix).detach()
        log_mag0 = torch.log1p(mags[0]).detach()
        log_mag2 = torch.log1p(mags[2]).detach()

        # grounding
        feat_sound_ground = self.net_sound_ground(log_mag_mix)

        feat_frames_ground = [None for n in range(N)]
        for n in range(N):
            feat_frames_ground[n] = self.net_frame_ground.forward_multiframe(frames[n])

        # Grounding for sep
        g_sep = [None for n in range(N)]
        x = [None for n in range(N)]
        for n in range(N):
            g_sep[n] = self.net_grounding(feat_sound_ground, feat_frames_ground[n])
            x[n] = torch.softmax(g_sep[n].clone(), dim=-1)

        # Grounding module
        #feat_frame = (feat_frames_ground[0] + feat_frames_ground[1]) * 0.5
        g_pos = self.net_grounding(self.net_sound_ground(log_mag0) , feat_frames_ground[0])
        g_pos1 = self.net_grounding(self.net_sound_ground(log_mag0), feat_frames_ground[1])
        g_neg = self.net_grounding(self.net_sound_ground(log_mag2), feat_frames_ground[0])

        # Grounding for solo sound
        g_solo = [None for n in range(N)]
        g_solo[0] = self.net_grounding(self.net_sound_ground(log_mag0), feat_frames_ground[0])
        g_solo[1] = self.net_grounding(self.net_sound_ground(log_mag0), feat_frames_ground[1])
        g_solo[2] = self.net_grounding(self.net_sound_ground(log_mag2), feat_frames_ground[2])
        g_solo[3] = self.net_grounding(self.net_sound_ground(log_mag2), feat_frames_ground[3])
        for n in range(N):
            g_solo[n] = torch.softmax(g_solo[n], dim=-1)
        g = [torch.softmax(g_pos, dim=-1), torch.softmax(g_neg, dim=-1), x, g_solo]

        # 1. forward net_sound -> BxCxHxW
        feat_sound = self.net_sound(log_mag_mix)
        feat_sound = activate(feat_sound, args.sound_activation)

        # 2. forward net_frame -> Bx1xC
        feat_frames = [None for n in range(N)]
        for n in range(N):
            feat_frames[n] = self.net_frame.forward_multiframe(frames[n])
            feat_frames[n] = activate(feat_frames[n], args.img_activation)

        # 3. sound synthesizer
        masks = [None for n in range(N)]
        for n in range(N):
            masks[n] = self.net_synthesizer(feat_frames[n], feat_sound)
            masks[n] = activate(masks[n], args.output_activation)

        # 4. adjusted masks with grounding confidence scores
        pred_masks = [None for n in range(N)]

        s1 = masks[1].size(2)
        s2 = masks[1].size(3)

        if args.testing:
            pred_masks[0] = masks[0]
            pred_masks[1] = masks[1]
            pred_masks[2] = masks[2]
            pred_masks[3] = masks[3]
        else:
            pred_masks[0] = torch.mul(tf_data(g_sep[0],B,s1,s2).round(), masks[0]) + torch.mul(tf_data(g_sep[1],B,s1,s2).round(), masks[1])
            pred_masks[1] = masks[1]
            pred_masks[2] = torch.mul(tf_data(g_sep[2],B,s1,s2).round(), masks[2]) + torch.mul(tf_data(g_sep[3],B,s1,s2).round(), masks[3])
            pred_masks[3] = masks[3]

        # 5. loss
        loss_sep = 0.5*(self.crit(pred_masks[0], gt_masks[0], weight).reshape(1) + self.crit(pred_masks[2], gt_masks[2], weight).reshape(1))


        p = torch.zeros(B).cuda()
        n = torch.ones(B).cuda()
        cts_pos = torch.zeros(B).cuda()
        cts_pos1 = torch.zeros(B).cuda()
        for i in range(B):
            cts_pos[i] = self.cts(g_pos[i:i + 1], p[i:i + 1].long())
            cts_pos1[i] = self.cts(g_pos1[i:i + 1], p[i:i + 1].long())


        loss_grd  = torch.min(cts_pos, cts_pos1).mean() + self.cts(g_neg, n.long()) + torch.min(self.cts(g_sep[0], p.long()), self.cts(g_sep[1], p.long())) + torch.min(self.cts(g_sep[2], p.long()), self.cts(g_sep[3], p.long()))

        err = loss_sep + 0.25*loss_grd

        return err, loss_sep, g, \
               {'pred_masks': pred_masks, 'gt_masks': gt_masks,
                'mag_mix': mag_mix, 'mags': mags, 'weight': weight}


# Calculate metrics
def calc_metrics(batch_data, outputs, args):
    # meters
    sdr_mix_meter = AverageMeter()
    sdr_meter = AverageMeter()
    sir_meter = AverageMeter()
    sar_meter = AverageMeter()

    # fetch data and predictions
    mag_mix = batch_data['mag_mix']
    phase_mix = batch_data['phase_mix']
    audios = batch_data['audios'][::2]

    pred_masks_ = outputs['pred_masks'][::2]

    # unwarp log scale
    N = 2#args.num_mix-1
    B = mag_mix.size(0)
    pred_masks_linear = [None for n in range(N)]
    for n in range(N):
        if args.log_freq:
            grid_unwarp = torch.from_numpy(
                warpgrid(B, args.stft_frame//2+1, pred_masks_[0].size(3), warp=False)).to(args.device)
            pred_masks_linear[n] = F.grid_sample(pred_masks_[n], grid_unwarp)
        else:
            pred_masks_linear[n] = pred_masks_[n]

    # convert into numpy
    mag_mix = mag_mix.numpy()
    phase_mix = phase_mix.numpy()
    for n in range(N):
        pred_masks_linear[n] = pred_masks_linear[n].detach().cpu().numpy()

        # threshold if binary mask
        if args.binary_mask:
            pred_masks_linear[n] = (pred_masks_linear[n] > args.mask_thres).astype(np.float32)

    # loop over each sample
    for j in range(B):
        # save mixture
        mix_wav = istft_reconstruction(mag_mix[j, 0], phase_mix[j, 0], hop_length=args.stft_hop)

        # save each component
        preds_wav = [None for n in range(N)]
        for n in range(N):
            # Predicted audio recovery
            pred_mag = mag_mix[j, 0] * pred_masks_linear[n][j, 0]
            preds_wav[n] = istft_reconstruction(pred_mag, phase_mix[j, 0], hop_length=args.stft_hop)

        # separation performance computes
        L = preds_wav[0].shape[0]
        gts_wav = [None for n in range(N)]
        valid = True
        for n in range(N):
            gts_wav[n] = audios[n][j, 0:L].numpy()
            valid *= np.sum(np.abs(gts_wav[n])) > 1e-5
            valid *= np.sum(np.abs(preds_wav[n])) > 1e-5
        if valid:
            sdr, sir, sar, _ = bss_eval_sources(
                np.asarray(gts_wav),
                np.asarray(preds_wav),
                False)
            sdr_mix, _, _, _ = bss_eval_sources(
                np.asarray(gts_wav),
                np.asarray([mix_wav[0:L] for n in range(N)]),
                False)
            sdr_mix_meter.update(sdr_mix.mean())
            sdr_meter.update(sdr.mean())
            sir_meter.update(sir.mean())
            sar_meter.update(sar.mean())

    return [sdr_mix_meter.average(),
            sdr_meter.average(),
            sir_meter.average(),
            sar_meter.average()]


# Visualize predictions
def output_visuals(vis_rows, batch_data, outputs, args):
    # fetch data and predictions
    mag_mix = batch_data['mag_mix']
    phase_mix = batch_data['phase_mix']
    frames = batch_data['frames']
    infos = batch_data['infos']

    pred_masks_ = outputs['pred_masks']
    gt_masks_ = outputs['gt_masks']
    mag_mix_ = outputs['mag_mix']
    weight_ = outputs['weight']

    # unwarp log scale
    N = args.num_mix#-1
    B = mag_mix.size(0)
    pred_masks_linear = [None for n in range(N)]
    gt_masks_linear = [None for n in range(N)]
    for n in range(N):
        if args.log_freq:
            grid_unwarp = torch.from_numpy(
                warpgrid(B, args.stft_frame//2+1, gt_masks_[0].size(3), warp=False)).to(args.device)
            pred_masks_linear[n] = F.grid_sample(pred_masks_[n], grid_unwarp)
            gt_masks_linear[n] = F.grid_sample(gt_masks_[n], grid_unwarp)
        else:
            pred_masks_linear[n] = pred_masks_[n]
            gt_masks_linear[n] = gt_masks_[n]

    # convert into numpy
    mag_mix = mag_mix.numpy()
    mag_mix_ = mag_mix_.detach().cpu().numpy()
    phase_mix = phase_mix.numpy()
    weight_ = weight_.detach().cpu().numpy()
    for n in range(N):
        pred_masks_[n] = pred_masks_[n].detach().cpu().numpy()
        pred_masks_linear[n] = pred_masks_linear[n].detach().cpu().numpy()
        gt_masks_[n] = gt_masks_[n].detach().cpu().numpy()
        gt_masks_linear[n] = gt_masks_linear[n].detach().cpu().numpy()

        # threshold if binary mask
        if args.binary_mask:
            pred_masks_[n] = (pred_masks_[n] > args.mask_thres).astype(np.float32)
            pred_masks_linear[n] = (pred_masks_linear[n] > args.mask_thres).astype(np.float32)

    # loop over each sample
    for j in range(B):
        row_elements = []

        # video names
        prefix = []
        for n in range(N):
            prefix.append('-'.join(infos[n][0][j].split('/')[-2:]).split('.')[0])
        prefix = '+'.join(prefix)
        makedirs(os.path.join(args.vis, prefix))

        # save mixture
        mix_wav = istft_reconstruction(mag_mix[j, 0], phase_mix[j, 0], hop_length=args.stft_hop)
        mix_amp = magnitude2heatmap(mag_mix_[j, 0])
        weight = magnitude2heatmap(weight_[j, 0], log=False, scale=100.)
        filename_mixwav = os.path.join(prefix, 'mix.wav')
        filename_mixmag = os.path.join(prefix, 'mix.jpg')
        filename_weight = os.path.join(prefix, 'weight.jpg')
        imsave(os.path.join(args.vis, filename_mixmag), mix_amp[::-1, :, :])
        imsave(os.path.join(args.vis, filename_weight), weight[::-1, :])
        wavfile.write(os.path.join(args.vis, filename_mixwav), args.audRate, mix_wav)
        row_elements += [{'text': prefix}, {'image': filename_mixmag, 'audio': filename_mixwav}]

        # save each component
        preds_wav = [None for n in range(N)]
        for n in range(N):

            # GT and predicted audio recovery
            gt_mag = mag_mix[j, 0] * gt_masks_linear[n][j, 0]
            gt_wav = istft_reconstruction(gt_mag, phase_mix[j, 0], hop_length=args.stft_hop)
            pred_mag = mag_mix[j, 0] * pred_masks_linear[n][j, 0]
            preds_wav[n] = istft_reconstruction(pred_mag, phase_mix[j, 0], hop_length=args.stft_hop)

            # output masks
            filename_gtmask = os.path.join(prefix, 'gtmask{}.jpg'.format(n+1))
            filename_predmask = os.path.join(prefix, 'predmask{}.jpg'.format(n+1))
            gt_mask = (np.clip(gt_masks_[n][j, 0], 0, 1) * 255).astype(np.uint8)
            pred_mask = (np.clip(pred_masks_[n][j, 0], 0, 1) * 255).astype(np.uint8)
            imsave(os.path.join(args.vis, filename_gtmask), gt_mask[::-1, :])
            imsave(os.path.join(args.vis, filename_predmask), pred_mask[::-1, :])

            # ouput spectrogram (log of magnitude, show colormap)
            filename_gtmag = os.path.join(prefix, 'gtamp{}.jpg'.format(n+1))
            filename_predmag = os.path.join(prefix, 'predamp{}.jpg'.format(n+1))
            gt_mag = magnitude2heatmap(gt_mag)
            pred_mag = magnitude2heatmap(pred_mag)
            imsave(os.path.join(args.vis, filename_gtmag), gt_mag[::-1, :, :])
            imsave(os.path.join(args.vis, filename_predmag), pred_mag[::-1, :, :])

            # output audio
            filename_gtwav = os.path.join(prefix, 'gt{}.wav'.format(n+1))
            filename_predwav = os.path.join(prefix, 'pred{}.wav'.format(n+1))
            wavfile.write(os.path.join(args.vis, filename_gtwav), args.audRate, gt_wav)
            wavfile.write(os.path.join(args.vis, filename_predwav), args.audRate, preds_wav[n])

            # output video
            frames_tensor = [recover_rgb(frames[n][j, :, t]) for t in range(args.num_frames)]
            frames_tensor = np.asarray(frames_tensor)
            path_video = os.path.join(args.vis, prefix, 'video{}.mp4'.format(n+1))
            save_video(path_video, frames_tensor, fps=args.frameRate/args.stride_frames)

            # combine gt video and audio
            filename_av = os.path.join(prefix, 'av{}.mp4'.format(n+1))
            combine_video_audio(
                path_video,
                os.path.join(args.vis, filename_gtwav),
                os.path.join(args.vis, filename_av))

            row_elements += [
                {'video': filename_av},
                {'image': filename_predmag, 'audio': filename_predwav},
                {'image': filename_gtmag, 'audio': filename_gtwav},
                {'image': filename_predmask},
                {'image': filename_gtmask}]

        row_elements += [{'image': filename_weight}]
        vis_rows.append(row_elements)


def evaluate(netWrapper, loader, history, epoch, args):
    print('Evaluating at {} epochs...'.format(epoch))
    torch.set_grad_enabled(False)

    # remove previous viz results
    makedirs(args.vis, remove=True)

    # switch to eval mode
    netWrapper.eval()

    # initialize meters
    loss_meter = AverageMeter()
    sdr_mix_meter = AverageMeter()
    sdr_meter = AverageMeter()
    sir_meter = AverageMeter()
    sar_meter = AverageMeter()

    # initialize HTML header
    visualizer = HTMLVisualizer(os.path.join(args.vis, 'index.html'))
    header = ['Filename', 'Input Mixed Audio']
    for n in range(1, args.num_mix+1):
        header += ['Video {:d}'.format(n),
                   'Predicted Audio {:d}'.format(n),
                   'GroundTruth Audio {}'.format(n),
                   'Predicted Mask {}'.format(n),
                   'GroundTruth Mask {}'.format(n)]
    header += ['Loss weighting']
    visualizer.add_header(header)
    vis_rows = []

    for i, batch_data in enumerate(loader):
        # forward pass
        err,_, g, outputs = netWrapper.forward(batch_data, args)
        err = err.mean()

        loss_meter.update(err.item())
        print('[Eval] iter {}, loss: {:.4f}'.format(i, err.item()))
        grd_acc = np.sum(
            np.round(g[0][:, 0].detach().cpu().numpy()) + (np.round(g[1][:, 1].detach().cpu().numpy()))) / (
                          2 * len(np.round(g[0][:, 0].detach().cpu().numpy())))
        grd_mix_acc = (np.sum(
            np.round(g[2][0][:, 0].detach().cpu().numpy()) + np.round(g[2][1][:, 1].detach().cpu().numpy())
            + np.round(g[2][2][:, 0].detach().cpu().numpy()) + (
                np.round(g[2][3][:, 1].detach().cpu().numpy())))) / (
                              4 * len(np.round(g[2][0][:, 0].detach().cpu().numpy())))

        grd_solo_acc = (np.sum(
            np.round(g[3][0][:, 0].detach().cpu().numpy()) + np.round(g[3][1][:, 1].detach().cpu().numpy())
            + np.round(g[3][2][:, 0].detach().cpu().numpy()) + (
                np.round(g[3][3][:, 1].detach().cpu().numpy())))) / (
                               4 * len(np.round(g[3][0][:, 0].detach().cpu().numpy())))

        print('Grounding acc {:.2f}, Solo Grounding acc: {:.2f}, Sep Grounding acc: {:.2f}'.format(grd_acc, grd_solo_acc, grd_mix_acc))

        # calculate metrics
        sdr_mix, sdr, sir, sar = calc_metrics(batch_data, outputs, args)
        #print(sir)

        sdr_mix_meter.update(sdr_mix)
        sdr_meter.update(sdr)
        sir_meter.update(sir)
        sar_meter.update(sar)

        # output visualization
        #if len(vis_rows) < args.num_vis:
        output_visuals(vis_rows, batch_data, outputs, args)


    print('[Eval Summary] Epoch: {}, Loss: {:.4f}, '
          'SDR_mixture: {:.4f}, SDR: {:.4f}, SIR: {:.4f}, SAR: {:.4f}'
          .format(epoch, loss_meter.average(),
                  sdr_mix_meter.average(),
                  sdr_meter.average(),
                  sir_meter.average(),
                  sar_meter.average()))
    history['val']['epoch'].append(epoch)
    history['val']['err'].append(loss_meter.average())
    history['val']['sdr'].append(sdr_meter.average())
    history['val']['sir'].append(sir_meter.average())
    history['val']['sar'].append(sar_meter.average())

    print('Plotting html for visualization...')
    visualizer.add_rows(vis_rows)
    visualizer.write_html()

    # Plot figure
    if epoch > 0:
        print('Plotting figures...')
        plot_loss_metrics(args.ckpt, history)


# train one epoch
def train(netWrapper, loader, optimizer, history, epoch, args):
    torch.set_grad_enabled(True)
    batch_time = AverageMeter()
    data_time = AverageMeter()
    # switch to train mode
    netWrapper.train()

    # main loop
    torch.cuda.synchronize()
    tic = time.perf_counter()
    for i, batch_data in enumerate(loader):
        # measure data time
        torch.cuda.synchronize()
        data_time.update(time.perf_counter() - tic)

        # forward pass
        netWrapper.zero_grad()
        err,loss,_, _ = netWrapper.forward(batch_data, args)
        err = err.mean()

        # backward
        err.backward()
        optimizer.step()

        # measure total time
        torch.cuda.synchronize()
        batch_time.update(time.perf_counter() - tic)
        tic = time.perf_counter()

        # display
        if i % args.disp_iter == 0:
            print('Epoch: [{}][{}/{}], Time: {:.2f}, Data: {:.2f}, '
                  'lr_sound: {}, lr_frame: {}, lr_synthesizer: {}, '
                  'loss: {:.4f}'
                  .format(epoch, i, args.epoch_iters,
                          batch_time.average(), data_time.average(),
                          args.lr_sound, args.lr_frame, args.lr_synthesizer,
                          err.item()))
            fractional_epoch = epoch - 1 + 1. * i / args.epoch_iters
            history['train']['epoch'].append(fractional_epoch)
            history['train']['err'].append(loss.mean().item())


def checkpoint(nets, history, epoch, args):
    print('Saving checkpoints at {} epochs.'.format(epoch))
    (net_sound_ground, net_frame_ground,
     net_sound, net_frame, net_synthesizer, net_grounding) = nets
    suffix_latest = 'latest.pth'
    suffix_best = 'best.pth'

    torch.save(history,
               '{}/history_{}'.format(args.ckpt, suffix_latest))
    torch.save(net_sound_ground.state_dict(),
               '{}/sound_ground_{}'.format(args.ckpt, suffix_latest))
    torch.save(net_frame_ground.state_dict(),
               '{}/frame_ground_{}'.format(args.ckpt, suffix_latest))
    torch.save(net_sound.state_dict(),
               '{}/sound_{}'.format(args.ckpt, suffix_latest))
    torch.save(net_frame.state_dict(),
               '{}/frame_{}'.format(args.ckpt, suffix_latest))
    torch.save(net_synthesizer.state_dict(),
               '{}/synthesizer_{}'.format(args.ckpt, suffix_latest))
    torch.save(net_grounding.state_dict(),
               '{}/grounding_{}'.format(args.ckpt, suffix_latest))

    cur_err = history['val']['err'][-1]
    cur_sdr = history['val']['sdr'][-1]
    if cur_err < args.best_err:
        args.best_err = cur_err
        torch.save(net_sound_ground.state_dict(),
                   '{}/sound_ground_{}'.format(args.ckpt, suffix_best))
        torch.save(net_frame_ground.state_dict(),
                   '{}/frame_ground_{}'.format(args.ckpt, suffix_best))
        torch.save(net_sound.state_dict(),
                   '{}/sound_{}'.format(args.ckpt, suffix_best))
        torch.save(net_frame.state_dict(),
                   '{}/frame_{}'.format(args.ckpt, suffix_best))
        torch.save(net_synthesizer.state_dict(),
                   '{}/synthesizer_{}'.format(args.ckpt, suffix_best))
        torch.save(net_grounding.state_dict(),
                   '{}/grounding_{}'.format(args.ckpt, suffix_best))


def create_optimizer(nets, args):
    (net_sound_ground, net_frame_ground, net_sound, net_frame, net_synthesizer, net_grounding) = nets
    param_groups = [{'params': net_sound_ground.parameters(), 'lr': args.lr_sound_ground},
                    {'params': net_sound.parameters(), 'lr': args.lr_sound},
                    {'params': net_synthesizer.parameters(), 'lr': args.lr_synthesizer},
                    {'params': net_grounding.parameters(), 'lr': args.lr_grounding},
                    {'params': net_frame.fc.parameters(), 'lr': args.lr_sound}]
    return torch.optim.Adam(param_groups)


def adjust_learning_rate(optimizer, args):
    args.lr_sound_ground *= 0.1
    args.lr_sound *= 0.1
    args.lr_frame *= 0.1
    args.lr_synthesizer *= 0.1
    args.lr_grounding *= 0.1
    for param_group in optimizer.param_groups:
        param_group['lr'] *= 0.1


def main(args):
    # Network Builders
    builder = ModelBuilder()
    net_sound_ground = builder.build_sound_ground(
        arch=args.arch_sound_ground,
        weights=args.weights_sound_ground)
    net_frame_ground = builder.build_frame_ground(
        arch=args.arch_frame_ground,
        pool_type=args.img_pool,
        weights=args.weights_frame_ground)
    net_sound = builder.build_sound(
        arch=args.arch_sound,
        fc_dim=args.num_channels,
        weights=args.weights_sound)
    net_frame = builder.build_frame(
        arch=args.arch_frame,
        fc_dim=args.num_channels,
        pool_type=args.img_pool,
        weights=args.weights_frame)
    net_synthesizer = builder.build_synthesizer(
        arch=args.arch_synthesizer,
        fc_dim=args.num_channels,
        weights=args.weights_synthesizer)
    net_grounding = builder.build_grounding(
        arch=args.arch_grounding,
        weights=args.weights_grounding)
    nets = (net_sound_ground, net_frame_ground,
            net_sound, net_frame, net_synthesizer, net_grounding)
    crit = builder.build_criterion(arch=args.loss)

    # Dataset and Loader
    dataset_train = MUSICMixDataset(
        args.list_train, args, split='train')
    dataset_val = MUSICMixDataset(
        args.list_val, args, max_sample=args.num_val, split=args.split)

    loader_train = torch.utils.data.DataLoader(
        dataset_train,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=int(args.workers),
        drop_last=True)
    loader_val = torch.utils.data.DataLoader(
        dataset_val,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=2,
        drop_last=False)
    args.epoch_iters = len(dataset_train) // args.batch_size
    print('1 Epoch = {} iters'.format(args.epoch_iters))

    # Wrap networks
    netWrapper = NetWrapper(nets, crit)
    netWrapper = torch.nn.DataParallel(netWrapper, device_ids=range(args.num_gpus))
    netWrapper.to(args.device)

    # Set up optimizer
    optimizer = create_optimizer(nets, args)

    # History of peroformance
    history = {
        'train': {'epoch': [], 'err': []},
        'val': {'epoch': [], 'err': [], 'sdr': [], 'sir': [], 'sar': []}}


    # Eval mode
    if args.mode == 'eval':
        args.testing = True
        evaluate(netWrapper, loader_val, history, 0, args)
        print('Evaluation Done!')
        return

    # Training loop
    for epoch in range(1, args.num_epoch + 1):
        train(netWrapper, loader_train, optimizer, history, epoch, args)

        # Evaluation and visualization
        if epoch % args.eval_epoch == 0:
            args.testing = True
            evaluate(netWrapper, loader_val, history, epoch, args)
            args.testing = False
            # checkpointing
            checkpoint(nets, history, epoch, args)

        # drop learning rate
        if epoch in args.lr_steps:
            adjust_learning_rate(optimizer, args)

    print('Training Done!')


if __name__ == '__main__':
    # arguments
    parser = ArgParser()
    args = parser.parse_train_arguments()
    args.batch_size = args.num_gpus * args.batch_size_per_gpu
    args.device = torch.device("cuda")

    # experiment name
    if args.mode == 'train':
        args.id += '-{}mix'.format(args.num_mix)
        if args.log_freq:
            args.id += '-LogFreq'
        args.id += '-{}-{}-{}'.format(
            args.arch_frame, args.arch_sound, args.arch_synthesizer)
        args.id += '-frames{}stride{}'.format(args.num_frames, args.stride_frames)
        args.id += '-{}'.format(args.img_pool)
        if args.binary_mask:
            assert args.loss == 'bce', 'Binary Mask should go with BCE loss'
            args.id += '-binary'
        else:
            args.id += '-ratio'
        if args.weighted_loss:
            args.id += '-weightedLoss'
        args.id += '-channels{}'.format(args.num_channels)
        args.id += '-epoch{}'.format(args.num_epoch)
        args.id += '-step' + '_'.join([str(x) for x in args.lr_steps])

    print('Model ID: {}'.format(args.id))

    # paths to save/load output
    args.ckpt = os.path.join(args.ckpt, args.id)
    args.vis = os.path.join(args.ckpt, 'visualization/')
    if args.mode == 'train':
        makedirs(args.ckpt, remove=True)
    elif args.mode == 'eval':
        args.weights_frame_ground = os.path.join(args.ckpt, 'frame_ground_best.pth')
        args.weights_sound_ground = os.path.join(args.ckpt, 'sound_ground_best.pth')
        args.weights_sound = os.path.join(args.ckpt, 'sound_best.pth')
        args.weights_frame = os.path.join(args.ckpt, 'frame_best.pth')
        args.weights_synthesizer = os.path.join(args.ckpt, 'synthesizer_best.pth')
        args.weights_grounding = os.path.join(args.ckpt, 'grounding_best.pth')

    # initialize best error with a big number
    args.best_err = float("inf")
    args.best_sdr = float("inf")
    args.testing = False

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    main(args)
