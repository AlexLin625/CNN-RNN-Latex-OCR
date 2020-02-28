import time
from config import *
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence
from model.model import *
from model.utils import *
# from model.dataloader import *
from model import metrics,dataloader

# device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
global device
device = "cpu"
cudnn.benchmark = False  


def main():
    """
    Training and validation.
    """

    global best_score, epochs_since_improvement, checkpoint, start_epoch, fine_tune_encoder, data_name, word_map

    # 字典文件
    word_map = load_json(vocab_path)

    # Initialize / load checkpoint
    if checkpoint is None:
        decoder = DecoderWithAttention(attention_dim=attention_dim,
                                       embed_dim=emb_dim,
                                       decoder_dim=decoder_dim,
                                       vocab_size=len(word_map),
                                       dropout=dropout)
        decoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, decoder.parameters()),
                                             lr=decoder_lr)
        encoder = Encoder()
        encoder_optimizer = None
        # encoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, encoder.parameters()),
        #                                      lr=encoder_lr) if fine_tune_encoder else None

    else:
        checkpoint = torch.load(checkpoint)
        start_epoch = checkpoint['epoch'] + 1
        epochs_since_improvement = checkpoint['epochs_since_improvement']
        best_score = checkpoint['score']
        decoder = checkpoint['decoder']
        decoder_optimizer = checkpoint['decoder_optimizer']
        encoder = checkpoint['encoder']
        # encoder_optimizer = checkpoint['encoder_optimizer']
        encoder_optimizer = None
        # if fine_tune_encoder is True and encoder_optimizer is None:
        #     encoder.fine_tune(fine_tune_encoder)
        #     encoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, encoder.parameters()),
        #                                          lr=encoder_lr)

    # Move to GPU, if available
    decoder = decoder.to(device)
    encoder = encoder.to(device)

    # 使用交叉熵损失函数
    criterion = nn.CrossEntropyLoss().to(device)

    # 自定义的数据集
    train_loader = dataloader.formuladataset('val1.json',batch_size = batch_size)
    val_loader = dataloader.formuladataset('test1.json',batch_size = test_batch_size)

    # Epochs
    for epoch in range(start_epoch, epochs):
        print('start epoch:',epoch)
        train_loader.shuffle()
        val_loader.shuffle()

        # 如果迭代4次后没有改善,则对学习率进行衰减,如果迭代20次都没有改善则触发早停.直到最大迭代次数
        if epochs_since_improvement == 20:
            break
        if epochs_since_improvement > 0 and epochs_since_improvement % 4 == 0:
            adjust_learning_rate(decoder_optimizer, 0.8)

        # One epoch's training
        train(train_loader=train_loader,
              encoder=encoder,
              decoder=decoder,
              criterion=criterion,
              decoder_optimizer=decoder_optimizer,
              epoch=epoch)#encoder_optimizer=encoder_optimizer,

        # One epoch's validation
        recent_score = validate(val_loader=val_loader,
                                encoder=encoder,
                                decoder=decoder,
                                criterion=criterion)

        # Check if there was an improvement
        is_best = recent_score > best_score
        best_score = max(recent_score, best_score)
        if not is_best:
            epochs_since_improvement += 1
            print("\nEpochs since last improvement: %d\n" % (epochs_since_improvement,))
        else:
            epochs_since_improvement = 0

        if epoch % save_freq == 0:
            print('Saveing...')
            save_checkpoint(data_name, epoch, epochs_since_improvement, encoder, decoder,
                        decoder_optimizer, recent_score, is_best)
        print('--------------------------------------------------------------------------')


def train(train_loader, encoder, decoder, criterion, decoder_optimizer, epoch):
    """
    Performs one epoch's training.
    :param train_loader: 训练集的dataloader
    :param encoder: encoder model
    :param decoder: decoder model
    :param criterion: 损失函数
    :param encoder_optimizer: optimizer to update encoder's weights (if fine-tuning)
    :param decoder_optimizer: optimizer to update decoder's weights
    :param epoch: epoch number
    """

    decoder.train()  # train mode (dropout and batchnorm is used)
    encoder.train()

    batch_time = AverageMeter()  # forward prop. + back prop. time
    data_time = AverageMeter()  # data loading time
    losses = AverageMeter()  # loss (per word decoded)
    top5accs = AverageMeter()  # top5 accuracy

    start = time.time()

    # Batches
    for i, (imgs, caps, caplens) in enumerate(train_loader):
        data_time.update(time.time() - start)

        # Move to GPU, if available
        imgs = imgs.to(device)
        caps = caps.to(device)
        caplens = caplens.to(device)

        # Forward prop.
        imgs = encoder(imgs)
        scores, caps_sorted, decode_lengths, alphas, sort_ind = decoder(imgs, caps, caplens)

        # 由于加入开始符<start>以及停止符<end>,caption从第二位开始,知道结束符
        targets = caps_sorted[:, 1:]

        # Remove timesteps that we didn't decode at, or are pads
        # pack_padded_sequence is an easy trick to do this
        # scores, _ = pack_padded_sequence(scores, decode_lengths, batch_first=True)
        # targets, _ = pack_padded_sequence(targets, decode_lengths, batch_first=True)
        scores = pack_padded_sequence(scores, decode_lengths, batch_first=True).data
        targets = pack_padded_sequence(targets, decode_lengths, batch_first=True).data

        # Calculate loss
        scores = scores.to(device)
        loss = criterion(scores, targets)

        # 加入 doubly stochastic attention 正则化
        loss += alpha_c * ((1. - alphas.sum(dim=1)) ** 2).mean()

        # 反向传播
        decoder_optimizer.zero_grad()
        loss.backward()

        # 梯度裁剪
        if grad_clip is not None:
            clip_gradient(decoder_optimizer, grad_clip)
            # if encoder_optimizer is not None:
            #     clip_gradient(encoder_optimizer, grad_clip)

        # 更新权重
        decoder_optimizer.step()
        # if encoder_optimizer is not None:
        #     encoder_optimizer.step()

        # Keep track of metrics
        top5 = accuracy(scores, targets, 5)
        losses.update(loss.item(), sum(decode_lengths))
        top5accs.update(top5, sum(decode_lengths))
        batch_time.update(time.time() - start)

        start = time.time()

        # Print status
        if i % print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Batch Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data Load Time {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Top-5 Accuracy {top5.val:.3f} ({top5.avg:.3f})'.format(epoch, i, len(train_loader),
                                                                          batch_time=batch_time,
                                                                          data_time=data_time, loss=losses,
                                                                          top5=top5accs))


def validate(val_loader, encoder, decoder, criterion):
    """
    Performs one epoch's validation.
    :param val_loader: 用于验证集的dataloader
    :param encoder: encoder model
    :param decoder: decoder model
    :param criterion: 损失函数
    :return: 验证集上的BLEU-4 score
    """
    decoder.eval()  # 推断模式,取消dropout以及批标准化
    if encoder is not None:
        encoder.eval()

    batch_time = AverageMeter()
    losses = AverageMeter()
    top5accs = AverageMeter()

    start = time.time()

    references = list()  # references (true captions) for calculating BLEU-4 score
    hypotheses = list()  # hypotheses (predictions)

    # explicitly disable gradient calculation to avoid CUDA memory error
    # solves the issue #57
    with torch.no_grad():
        # Batches
        # for i, (imgs, caps, caplens, allcaps) in enumerate(val_loader):
        for i, (imgs, caps, caplens) in enumerate(val_loader):

            # Move to device, if available
            imgs = imgs.to(device)
            caps = caps.to(device)
            caplens = caplens.to(device)

            # Forward prop.
            if encoder is not None:
                imgs = encoder(imgs)
            scores, caps_sorted, decode_lengths, alphas, sort_ind = decoder(imgs, caps, caplens)

            # Since we decoded starting with <start>, the targets are all words after <start>, up to <end>
            targets = caps_sorted[:, 1:]

            # Remove timesteps that we didn't decode at, or are pads
            # pack_padded_sequence is an easy trick to do this
            scores_copy = scores.clone()
            scores = pack_padded_sequence(scores, decode_lengths, batch_first=True).data
            targets = pack_padded_sequence(targets, decode_lengths, batch_first=True).data

            # Calculate loss
            loss = criterion(scores, targets)

            # Add doubly stochastic attention regularization
            loss += alpha_c * ((1. - alphas.sum(dim=1)) ** 2).mean()

            # Keep track of metrics
            losses.update(loss.item(), sum(decode_lengths))
            top5 = accuracy(scores, targets, 5)
            top5accs.update(top5, sum(decode_lengths))
            batch_time.update(time.time() - start)

            start = time.time()

            if i % print_freq == 0:
                print('Validation: [{0}/{1}]\t'
                      'Batch Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Top-5 Accuracy {top5.val:.3f} ({top5.avg:.3f})\t'.format(i, len(val_loader), batch_time=batch_time,
                                                                                loss=losses, top5=top5accs))

            # Store references (true captions), and hypothesis (prediction) for each image
            # If for n images, we have n hypotheses, and references a, b, c... for each image, we need -
            # references = [[ref1a, ref1b, ref1c], [ref2a, ref2b], ...], hypotheses = [hyp1, hyp2, ...]

            # References
            # allcaps = allcaps[sort_ind]  # because images were sorted in the decoder
            # for j in range(allcaps.shape[0]):
            #     img_caps = allcaps[j].tolist()
            #     img_captions = list(
            #         map(lambda c: [w for w in c if w not in {word_map['<start>'], word_map['<pad>']}],
            #             img_caps))  # remove <start> and pads
            #     references.append(img_captions)
            caplens = caplens[sort_ind]
            caps = caps[sort_ind]
            for i in range(len(caplens)):
                references.append(caps[i][1:caplens[i]].tolist())
            # Hypotheses
            # 这里直接使用greedy模式进行评价,在推断中一般使用集束搜索模式
            _, preds = torch.max(scores_copy, dim=2)
            preds = preds.tolist()
            temp_preds = list()
            for j, p in enumerate(preds):
                temp_preds.append(preds[j][:decode_lengths[j]])  # remove pads
            preds = temp_preds
            hypotheses.extend(preds)

            assert len(references) == len(hypotheses)

            Score = metrics.evaluate(losses, top5accs, references, hypotheses)
    return Score


if __name__ == '__main__':
    main()