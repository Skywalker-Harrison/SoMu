from __future__ import absolute_import, division, print_function, unicode_literals
import logging
import math
import os
import sys

import opencc
import torch
from torch import nn
from torch.nn import CrossEntropyLoss, MSELoss

from transformers.modeling_bert import *
from utils import pho_convertor, pho2_convertor, pos_convertor, wubi_convertor
from copy import deepcopy
from PIL import ImageFont
import numpy as np

from char_cnn import CharResNet, CharResNet1

def _is_chinese_char(cp):
    if ((cp >= 0x4E00 and cp <= 0x9FFF) or  #
                (cp >= 0x3400 and cp <= 0x4DBF) or  #
                (cp >= 0x20000 and cp <= 0x2A6DF) or  #
                (cp >= 0x2A700 and cp <= 0x2B73F) or  #
                (cp >= 0x2B740 and cp <= 0x2B81F) or  #
                (cp >= 0x2B820 and cp <= 0x2CEAF) or
                (cp >= 0xF900 and cp <= 0xFAFF) or  #
                (cp >= 0x2F800 and cp <= 0x2FA1F)):  #
        return True
    return False

class SpellBert(BertPreTrainedModel):
    def __init__(self, config):
        super(SpellBert, self).__init__(config)

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    @staticmethod
    def build_batch(batch, tokenizer):
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        outputs = self.bert(input_ids, attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs

        return outputs 

class SpellBertPho1(BertPreTrainedModel):
    def __init__(self, config):
        super(SpellBertPho1, self).__init__(config)

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        self.integrate = nn.Linear(2*config.hidden_size, config.hidden_size)
        out_config = deepcopy(config)
        out_config.num_hidden_layers = 2
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    @staticmethod
    def build_batch(batch, tokenizer):
        input_shape = batch['src_idx'].size()
        pho_idx_1, pho_idx_2, pho_idx_3 = [], [], []
        for i in range(input_shape[0]):
            pho1 = [0] * input_shape[1]
            pho2 = [0] * input_shape[1]
            pho3 = [0] * input_shape[1]
            token_ids = batch['src_idx'][i][1:batch['lengths'][i]+1].numpy().tolist()
            tokens = tokenizer.convert_ids_to_tokens(token_ids)
            
            for p, (v1, v2, v3) in zip(range(1, len(tokens)+1), pho_convertor.convert(tokens)):
                pho1[p] = v1
                pho2[p] = v2
                pho3[p] = v3
            pho_idx_1.append(pho1)
            pho_idx_2.append(pho2)
            pho_idx_3.append(pho3)
            
        batch['pho_idx_1'] = torch.tensor(pho_idx_1, dtype=torch.long)
        batch['pho_idx_2'] = torch.tensor(pho_idx_2, dtype=torch.long)
        batch['pho_idx_3'] = torch.tensor(pho_idx_3, dtype=torch.long)
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx_1 = batch['pho_idx_1']
        pho_idx_2 = batch['pho_idx_2']
        pho_idx_3 = batch['pho_idx_3']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        bert_outputs = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx_1)
        pho_embeddings += self.pho_embeddings(pho_idx_2)
        pho_embeddings += self.pho_embeddings(pho_idx_3)
        pho_outputs = self.pho_model(inputs_embeds=pho_embeddings, attention_mask=attention_mask)[0]

        concated_outputs = torch.cat((bert_outputs, pho_outputs), dim=-1)
        concated_outputs = self.integrate(concated_outputs)

        outputs = self.output_block(inputs_embeds=concated_outputs,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs
        return outputs 

class SpellBertPho2(BertPreTrainedModel):
    def __init__(self, config):
        super(SpellBertPho2, self).__init__(config)

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        self.integrate = nn.Linear(2*config.hidden_size, config.hidden_size)
        out_config = deepcopy(config)
        out_config.num_hidden_layers = 2
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        input_shape = input_ids.size()

        bert_outputs = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_outputs = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        concated_outputs = torch.cat((bert_outputs, pho_outputs), dim=-1)
        concated_outputs = self.integrate(concated_outputs)

        outputs = self.output_block(inputs_embeds=concated_outputs,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs
        return outputs 

class SpellBertPho1Res(BertPreTrainedModel):
    def __init__(self, config):
        super(SpellBertPho1Res, self).__init__(config)

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.char_images = nn.Embedding(config.vocab_size, 1024)
        self.char_images.weight.requires_grad = False

        self.pho_embeddings = nn.Embedding(pho_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.resnet = CharResNet()
        pho_res_config = deepcopy(config)
        pho_res_config.num_hidden_layers = 4
        self.pho_res_model = BertModel(pho_res_config)

        self.integrate = nn.Linear(2*config.hidden_size, config.hidden_size)
        out_config = deepcopy(config)
        out_config.num_hidden_layers = 2
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    @staticmethod
    def build_batch(batch, tokenizer):
        input_shape = batch['src_idx'].size()
        pho_idx_1, pho_idx_2, pho_idx_3 = [], [], []
        for i in range(input_shape[0]):
            pho1 = [0] * input_shape[1]
            pho2 = [0] * input_shape[1]
            pho3 = [0] * input_shape[1]
            token_ids = batch['src_idx'][i][1:batch['lengths'][i]+1].numpy().tolist()
            tokens = tokenizer.convert_ids_to_tokens(token_ids)
            
            for p, (v1, v2, v3) in zip(range(1, len(tokens)+1), pho_convertor.convert(tokens)):
                pho1[p] = v1
                pho2[p] = v2
                pho3[p] = v3
            pho_idx_1.append(pho1)
            pho_idx_2.append(pho2)
            pho_idx_3.append(pho3)
            
        batch['pho_idx_1'] = torch.tensor(pho_idx_1, dtype=torch.long)
        batch['pho_idx_2'] = torch.tensor(pho_idx_2, dtype=torch.long)
        batch['pho_idx_3'] = torch.tensor(pho_idx_3, dtype=torch.long)
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx_1 = batch['pho_idx_1']
        pho_idx_2 = batch['pho_idx_2']
        pho_idx_3 = batch['pho_idx_3']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        bert_outputs = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx_1)
        pho_embeddings += self.pho_embeddings(pho_idx_2)
        pho_embeddings += self.pho_embeddings(pho_idx_3)
        
        src_idxs = input_ids.view(-1)
        images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        res_embeddings = self.resnet(images)
        res_embeddings = res_embeddings.reshape(input_ids.shape[0], input_ids.shape[1], -1).contiguous()
        pho_res_embeddings = pho_embeddings + res_embeddings
        pho_res_outputs = self.pho_res_model(inputs_embeds=pho_res_embeddings, attention_mask=attention_mask)[0]

        concated_outputs = torch.cat((bert_outputs, pho_res_outputs), dim=-1)
        concated_outputs = self.integrate(concated_outputs)

        outputs = self.output_block(inputs_embeds=concated_outputs,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs
        return outputs

class SpellBertPho2Res(BertPreTrainedModel):
    def __init__(self, config):
        super(SpellBertPho2Res, self).__init__(config)

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.char_images = nn.Embedding(config.vocab_size, 1024)
        self.char_images.weight.requires_grad = False

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        self.resnet = CharResNet()
        pho_res_config = deepcopy(config)
        pho_res_config.num_hidden_layers = 4
        self.pho_res_model = BertModel(pho_res_config)

        self.integrate = nn.Linear(2*config.hidden_size, config.hidden_size)
        out_config = deepcopy(config)
        out_config.num_hidden_layers = 2
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        input_shape = input_ids.size()

        bert_outputs = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        
        src_idxs = input_ids.view(-1)
        images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_res_embeddings = pho_hiddens + res_hiddens
        pho_res_outputs = self.pho_res_model(inputs_embeds=pho_res_embeddings, attention_mask=attention_mask)[0]

        concated_outputs = torch.cat((bert_outputs, pho_res_outputs), dim=-1)
        concated_outputs = self.integrate(concated_outputs)

        outputs = self.output_block(inputs_embeds=concated_outputs,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs
        return outputs 

class SpellBertPho2ResArch2(BertPreTrainedModel):
    def __init__(self, config):
        super(SpellBertPho2ResArch2, self).__init__(config)

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        self.char_images = nn.Embedding(config.vocab_size, 1024)
        self.char_images.weight.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet()
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.integrate = nn.Linear(3*config.hidden_size, config.hidden_size)
        out_config = deepcopy(config)
        out_config.num_hidden_layers = 2
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        input_shape = input_ids.size()

        bert_outputs = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)
        images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)

        concated_outputs = torch.cat((bert_outputs, pho_hiddens, res_hiddens), dim=-1)
        concated_outputs = self.integrate(concated_outputs)

        outputs = self.output_block(inputs_embeds=concated_outputs,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs
        return outputs 


class SpellBertPho2ResArch3(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch3, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.gate_net = nn.Linear(4*config.hidden_size, 3)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 3
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/wtl/research/ReaLiSe/simhei.ttf', False),
            ('/home/wtl/research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/wtl/research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/wtl/research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)

        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, bert_hiddens_mean), dim=-1)
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + (hiddens,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs
        return outputs 



class SpellBertPho2ResArch3MLM(BertPreTrainedModel):
    def __init__(self, config):
        super(SpellBertPho2ResArch3MLM, self).__init__(config)

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        self.char_images = nn.Embedding(config.vocab_size, 1024)
        self.char_images.weight.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet()
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.gate_net = nn.Linear(4*config.hidden_size, 3)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 3
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.cls = BertOnlyMLMHead(config)
        self.init_weights()

    def tie_cls_weight(self):
        #self.classifier.weight = self.bert.embeddings.word_embeddings.weight
        pass
        
    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)
        images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)

        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, bert_hiddens_mean), dim=-1)
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.cls(sequence_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs
        return outputs 


class SpellBertPho2ResArch4(BertPreTrainedModel):
    def __init__(self, config):
        super(SpellBertPho2ResArch4, self).__init__(config)

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        self.char_images = nn.Embedding(config.vocab_size, 1024)
        self.char_images.weight.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet()
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.gate_net = nn.Linear(4*config.hidden_size, 3)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 3
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)
        images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)

        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, bert_hiddens_mean), dim=-1)
        gated_values = self.gate_net(concated_outputs)
        gated_values = nn.functional.softmax(gated_values, dim=-1)
        # B * S * 3
        g0 = gated_values[:,:,0].unsqueeze(-1)
        g1 = gated_values[:,:,1].unsqueeze(-1)
        g2 = gated_values[:,:,2].unsqueeze(-1)
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs
        return outputs 

class SpellBertPho2ResArch5(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch5, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)
        self.pic_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pic_config = deepcopy(config)
        pic_config.num_hidden_layers = 4
        self.pic_model = BertModel(pic_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.gate_net = nn.Linear(4*config.hidden_size, 3)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 3
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/jhliang/Research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images).unsqueeze(1)
        res_hiddens = torch.nn.utils.rnn.pack_padded_sequence(
            input=res_hiddens,
            lengths=[1]*(input_shape[0]*input_shape[1]),
            batch_first=True,
            enforce_sorted=False,
        )
        #Add a gru layer to build up relationships. 
        _,res_hiddens = self.pic_gru(res_hiddens)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.pic_model(inputs_embeds=res_hiddens, attention_mask=attention_mask)[0]
        # res_hiddens = self.resnet_layernorm(res_hiddens)

        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, bert_hiddens_mean), dim=-1)
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs
        return outputs 

class Pho2ResPretrain(BertPreTrainedModel):
    def __init__(self, config):
        super(Pho2ResPretrain, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size

        self.char_images = nn.Embedding(config.vocab_size, 1024)
        self.char_images.weight.requires_grad = False

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        self.resnet = CharResNet()
        pho_res_config = deepcopy(config)
        pho_res_config.num_hidden_layers = 4
        self.pho_res_model = BertModel(pho_res_config)

        self.cls2 = BertOnlyMLMHead(config)

        self.init_weights()

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['tgt_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['tgt_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']

        input_shape = input_ids.size()
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        
        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_res_embeddings = pho_hiddens + res_hiddens
        sequence_output = self.pho_res_model(inputs_embeds=pho_res_embeddings, attention_mask=attention_mask)[0]

        prediction_scores = self.cls2(sequence_output)

        loss_fct = CrossEntropyLoss()
        # Only keep active parts of the loss
        active_loss = loss_mask.view(-1) == 1
        active_logits = prediction_scores.view(-1, self.vocab_size)[active_loss]
        active_labels = src_idxs[active_loss]
        loss = loss_fct(active_logits, active_labels)
        outputs = (loss, active_logits.argmax(dim=-1), active_labels, )
        return outputs 

class Pho2Pretrain(BertPreTrainedModel):
    def __init__(self, config):
        super(Pho2Pretrain, self).__init__(config)

        self.vocab_size = config.vocab_size

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        self.cls2 = BertOnlyMLMHead(config)

        self.init_weights()

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['tgt_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['tgt_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']

        input_shape = input_ids.size()
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        sequence_output = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        prediction_scores = self.cls2(sequence_output)

        loss_fct = CrossEntropyLoss()
        # Only keep active parts of the loss
        active_loss = loss_mask.view(-1) == 1
        active_logits = prediction_scores.view(-1, self.vocab_size)[active_loss]
        active_labels = input_ids.view(-1)[active_loss]
        loss = loss_fct(active_logits, active_labels)
        outputs = (loss, active_logits.argmax(dim=-1), active_labels, )
        return outputs 

class WubiPretrain(BertPreTrainedModel):
    def __init__(self, config):
        super(WubiPretrain, self).__init__(config)

        self.vocab_size = config.vocab_size

        self.wubi_embeddings = nn.Embedding(wubi_convertor.get_wubi_size(), config.hidden_size, padding_idx=0)
        self.wubi_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        wubi_config = deepcopy(config)
        wubi_config.num_hidden_layers = 4
        self.wubi_model = BertModel(wubi_config)

        self.cls2 = BertOnlyMLMHead(config)

        self.init_weights()

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['tgt_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        wubi_idx, wubi_lens = wubi_convertor.convert(chars)
        batch['wubi_idx'] = wubi_idx
        batch['wubi_lens'] = wubi_lens
        return batch

    def forward(self, batch):
        input_ids = batch['tgt_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        wubi_idx = batch['wubi_idx']
        wubi_lens = batch['wubi_lens']

        input_shape = input_ids.size()
        
        wubi_embeddings = self.wubi_embeddings(wubi_idx)
        wubi_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=wubi_embeddings,
            lengths=wubi_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, wubi_hiddens = self.wubi_gru(wubi_embeddings)
        wubi_hiddens = wubi_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        sequence_output = self.wubi_model(inputs_embeds=wubi_hiddens, attention_mask=attention_mask)[0]

        prediction_scores = self.cls2(sequence_output)

        loss_fct = CrossEntropyLoss()
        # Only keep active parts of the loss
        active_loss = loss_mask.view(-1) == 1
        active_logits = prediction_scores.view(-1, self.vocab_size)[active_loss]
        active_labels = input_ids.view(-1)[active_loss]
        loss = loss_fct(active_logits, active_labels)
        outputs = (loss, active_logits.argmax(dim=-1), active_labels, )
        return outputs 



class ResPretrain(BertPreTrainedModel):
    def __init__(self, config):
        super(ResPretrain, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.cls3 = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('simhei.ttf', False),
            ('xiaozhuan.ttf', False),
            ('simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        return batch

    def forward(self, batch):
        input_ids = batch['input_ids'] # (N, )        

        if self.config.num_fonts == 1:
            images = self.char_images(input_ids).reshape(input_ids.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=input_ids)

        res_hiddens = self.resnet(images)
        res_hiddens = self.dropout(res_hiddens)        
        prediction_scores = self.cls3(res_hiddens)

        loss_fct = CrossEntropyLoss()
        loss = loss_fct(prediction_scores, input_ids)
        outputs = (loss, prediction_scores.argmax(dim=-1), input_ids, )
        return outputs 


class SpellBertPho2ResArch3Pos(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch3Pos, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        self.pos_embeddings = nn.Embedding(pos_convertor.get_pos_size(), config.hidden_size, padding_idx=0)
        self.pos_gru=nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pos_config = deepcopy(config)
        pos_config.num_hidden_layers = 4
        self.pos_model = BertModel(pos_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.gate_net = nn.Linear(5*config.hidden_size, 4)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 3
        out_config.hidden_size = config.hidden_size

        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)
        self.pos_classifier = nn.Linear(config.hidden_size, pos_convertor.get_pos_size())

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight
        

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/jhliang/Research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        pos_idx, pos_lens = pos_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        batch['pos_idx'] = pos_idx
        batch['pos_lens'] = pos_lens
        tgt_chars = tokenizer.convert_ids_to_tokens(batch['tgt_idx'].flatten().tolist())
        tgt_pos_idx, tgt_pos_lens = pos_convertor.convert(tgt_chars)
        
        batch['tgt_pos_idx'] = tgt_pos_idx
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        pos_idx = batch['pos_idx']
        pos_lens = batch['pos_lens']

        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None
        label_pos_ids= batch['tgt_pos_idx'] if 'tgt_pos_idx' in batch else None
        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)


        pos_idx = pos_idx.unsqueeze(1)
        pos_embeddings = self.pos_embeddings(pos_idx)
        pos_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pos_embeddings,
            lengths=pos_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pos_hiddens = self.pos_gru(pos_embeddings)
        pos_hiddens = pos_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pos_hiddens = self.pos_model(inputs_embeds=pos_hiddens, attention_mask=attention_mask)[0]




        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)


        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, pos_hiddens, bert_hiddens_mean), dim=-1)
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        g3 = torch.sigmoid(gated_values[:,:,3].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens + g3* pos_hiddens
        # hiddens : [bsz, seq_len, hidden_size]
        # pos_hiddens: [bsz, seq_len, hidden_size]

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        #pos_logits = self.classifier(sequence_output)

        outputs = (logits,) + (hiddens,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            char_loss = loss_fct(active_logits, active_labels)
            
            # active_pos_logits = pos_logits.view(-1, pos_convertor.get_pos_size())[active_loss]
            # active_pos_labels = label_pos_ids.view(-1)[active_loss]
            # pos_loss = loss_fct(active_pos_logits, active_pos_labels)

            loss = char_loss

            outputs = (loss,) + outputs
        return outputs 


class SpellBertPho2ResArch3PosLoss(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch3PosLoss, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        self.pos_embeddings = nn.Embedding(pos_convertor.get_pos_size(), config.hidden_size, padding_idx=0)
        self.pos_gru=nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pos_config = deepcopy(config)
        pos_config.num_hidden_layers = 4
        self.pos_model = BertModel(pos_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.gate_net = nn.Linear(5*config.hidden_size, 4)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 3
        out_config.hidden_size = config.hidden_size

        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)
        self.pos_classifier = nn.Linear(config.hidden_size, pos_convertor.get_pos_size())

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight


    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/jhliang/Research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        pos_idx, pos_lens = pos_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        batch['pos_idx'] = pos_idx
        batch['pos_lens'] = pos_lens
        tgt_chars = tokenizer.convert_ids_to_tokens(batch['tgt_idx'].flatten().tolist())
        tgt_pos_idx, tgt_pos_lens = pos_convertor.convert(tgt_chars)
        
        batch['tgt_pos_idx'] = tgt_pos_idx
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        pos_idx = batch['pos_idx']
        pos_lens = batch['pos_lens']

        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None
        label_pos_ids= batch['tgt_pos_idx'] if 'tgt_pos_idx' in batch else None
        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)


        pos_idx = pos_idx.unsqueeze(1)
        pos_embeddings = self.pos_embeddings(pos_idx)
        # pos_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
        #     input=pos_embeddings,
        #     lengths=pos_lens,
        #     batch_first=True,
        #     enforce_sorted=False,
        # )
        
        # _, pos_hiddens = self.pos_gru(pos_embeddings)
        pos_hiddens = pos_embeddings.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pos_hiddens = self.pos_model(inputs_embeds=pos_hiddens, attention_mask=attention_mask)[0]




        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, pos_hiddens, bert_hiddens_mean), dim=-1)
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        g3 = torch.sigmoid(gated_values[:,:,3].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens + g3* pos_hiddens
        # hiddens : [bsz, seq_len, hidden_size]
        # pos_hiddens: [bsz, seq_len, hidden_size]

        
        outputs = self.output_block(inputs_embeds=hiddens,
                                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        pos_logits = self.pos_classifier(sequence_output)

        outputs = (logits,) + (hiddens,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            char_loss = loss_fct(active_logits, active_labels)

            active_pos_logits = pos_logits.view(-1, pos_convertor.get_pos_size())[active_loss]
            active_pos_labels = label_pos_ids.view(-1)[active_loss]
            pos_loss = loss_fct(active_pos_logits, active_pos_labels)

            loss = char_loss + pos_loss

            outputs = (loss,) + outputs
        return outputs 

class SpellBertPho2ResArch6(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch6, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        self.wubi_embeddings = nn.Embedding(wubi_convertor.get_wubi_size(),config.hidden_size,padding_idx=0)
        self.wubi_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        wubi_config = deepcopy(config)
        wubi_config.num_hidden_layers = 4
        self.wubi_model = BertModel(wubi_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)



        self.gate_net = nn.Linear(5*config.hidden_size, 4)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 3
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/jhliang/Research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        wubi_idx, wubi_lens = wubi_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        batch['wubi_idx'] = wubi_idx
        batch['wubi_lens'] = wubi_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        wubi_idx = batch['wubi_idx']
        wubi_lens = batch['wubi_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        wubi_embeddings = self.wubi_embeddings(wubi_idx)
        wubi_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=wubi_embeddings,
            lengths=wubi_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, wubi_hiddens = self.wubi_gru(wubi_embeddings)
        wubi_hiddens = wubi_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        wubi_hiddens = self.wubi_model(inputs_embeds=wubi_hiddens, attention_mask=attention_mask)[0]


        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)



        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, wubi_hiddens, bert_hiddens_mean), dim=-1)
        gated_values = self.gate_net(concated_outputs)
        # B * S * 4
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        g3 = torch.sigmoid(gated_values[:,:,3].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens + g3* wubi_hiddens

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + (hiddens, ) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            outputs = (loss,) + outputs
        return outputs 



class SpellBertPho2ResArch3Contrast(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch3Contrast, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.gate_net = nn.Linear(4*config.hidden_size, 3)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 3
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/jhliang/Research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        
        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)

        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, bert_hiddens_mean), dim=-1)
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)
        
        outputs = (logits,) + (hiddens,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)
            # outputs = (loss,) + outputs
        
            # Get negative samples from the same batch
            sentence_hiddens = hiddens.clone().mean(dim=1)
            negative_hiddens = hiddens.clone().mean(dim=1)
            # negative_hiddens = negative_hiddens.mean(dim=1)
            negative_hiddens = negative_hiddens.view(-1, hiddens.size(-1))
            negative_hiddens = negative_hiddens[torch.randperm(negative_hiddens.size(0))].view(negative_hiddens.size())
            
            # Calculate contrastive loss using InfoNCE
            temperature = 0.9  # Temperature parameter for scaling logits
            batch_size = hiddens.size(0)

            # Normalize the hidden vectors
            sentence_hiddens_normalized = torch.nn.functional.normalize(sentence_hiddens, dim=-1)
            negative_hiddens_normalized = torch.nn.functional.normalize(negative_hiddens, dim=-1)

            # Calculate similarity scores (dot product) between positive and negative samples
            similarity_scores = torch.matmul(sentence_hiddens_normalized, negative_hiddens_normalized.transpose(0, 1))

            # Calculate the logits by dividing the similarity scores by the temperature
            logits = similarity_scores / temperature

            # Create target labels (1 for positive samples, 0 for negative samples)
            targets = torch.arange(batch_size, device=hiddens.device)

            # Calculate the contrastive loss using InfoNCE loss
        
            contrastive_loss = torch.nn.functional.cross_entropy(logits, targets)


            total_loss = loss + 0.1 * contrastive_loss
            outputs = (total_loss,) + outputs
        return outputs


class SpellBertPho2ResArch3SoftMask(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch3SoftMask, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)


        self.bert_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=2,
            batch_first=True,
            dropout=0,
            bidirectional=True
        )
        self.detect_classifier = nn.Linear(config.hidden_size*2,2) #Correct and incorrect

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.gate_net = nn.Linear(4*config.hidden_size, 3)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 6
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)
        self.detect_classifier = nn.Linear(2*config.hidden_size,2)
        self.alpha = 0.8
        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/jhliang/research/ReaLiSe/simhei.ttf', False),
            ('/home/jhliang/research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/jhliang/research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/jhliang/research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None
        detect_label_ids = torch.where(input_ids == label_ids, torch.tensor(0).to(input_ids.device), torch.tensor(1).to(input_ids.device))

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        # bert_hiddens [bsz,max_len,hid_dim]
        bert_detect_hiddens = self.bert_gru(bert_hiddens)[0]
        # bert_detect_hiddens [bsz,max_len,hid_dim*2]
        detect_logits = self.detect_classifier(bert_detect_hiddens)
        # detect_logits [bsz,max_len,2]

        detect_logits = torch.sigmoid(detect_logits)


        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)

        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, bert_hiddens_mean), dim=-1)
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens

        mask_embedding = torch.zeros_like(hiddens).to(hiddens.device)
        expanded_detect_logits = detect_logits[:,:,0].unsqueeze(-1).expand_as(hiddens)
        hiddens = hiddens * expanded_detect_logits + mask_embedding * (1-expanded_detect_logits)
        

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output += bert_hiddens # residual connection

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)
        outputs = (logits,) + (hiddens,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:

            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)



            # Add detection errors here
            active_detect_loss = loss_mask.view(-1) == 1
            active_detect_logits = detect_logits.view(-1, 2)[active_detect_loss]
            active_detect_labels = detect_label_ids.view(-1)[active_detect_loss]
            detect_loss = loss_fct(active_detect_logits, active_detect_labels)

            total_loss = self.alpha * loss + (1-self.alpha) * detect_loss 
            outputs = (total_loss,) + outputs
        return outputs 


class SpellBertPho2ResArch3SoftMaskArch2(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch3SoftMaskArch2, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)


        self.hiddens_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=2,
            batch_first=True,
            dropout=0,
            bidirectional=True
        )
        self.detect_classifier = nn.Linear(config.hidden_size*2,2) #Correct and incorrect

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.gate_net = nn.Linear(4*config.hidden_size, 3)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 6
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)
        self.detect_classifier = nn.Linear(2*config.hidden_size,2)
        self.alpha = 0.8
        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/jhliang/Research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/jhliang/Research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None
        detect_label_ids = torch.where(input_ids == label_ids, torch.tensor(0).to(input_ids.device), torch.tensor(1).to(input_ids.device))

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        # bert_hiddens [bsz,max_len,hid_dim]


        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)

        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, bert_hiddens_mean), dim=-1)
        

        
        
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens

        #用hiddens来检测错误

        hiddens_detect = self.hiddens_gru(hiddens)[0]
        detect_logits = self.detect_classifier(hiddens_detect)
        detect_logits = torch.sigmoid(detect_logits)

        mask_embedding = torch.zeros_like(hiddens).to(hiddens.device)
        expanded_detect_logits = detect_logits[:,:,0].unsqueeze(-1).expand_as(hiddens)
        hiddens = hiddens * expanded_detect_logits + mask_embedding * (1-expanded_detect_logits)
        

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output += hiddens # residual connection

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)
        outputs = (logits,) + (hiddens,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:

            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)



            # Add detection errors here
            active_detect_loss = loss_mask.view(-1) == 1
            active_detect_logits = detect_logits.view(-1, 2)[active_detect_loss]
            active_detect_labels = detect_label_ids.view(-1)[active_detect_loss]
            detect_loss = loss_fct(active_detect_logits, active_detect_labels)

            total_loss = self.alpha * loss + (1-self.alpha) * detect_loss 
            outputs = (total_loss,) + outputs
        return outputs 


class SpellBertPho2ResArch3SoftMaskArch3(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch3SoftMaskArch3, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)


        self.hiddens_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=2,
            batch_first=True,
            dropout=0,
            bidirectional=True
        )
        self.detect_classifier = nn.Linear(config.hidden_size*2,2) #Correct and incorrect

        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.gate_net = nn.Linear(4*config.hidden_size, 3)

        out_config = deepcopy(config)
        out_config.num_hidden_layers = 6
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)
        self.detect_classifier = nn.Linear(2*config.hidden_size,2)
        self.alpha = 0
        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/wtl/research/ReaLiSe/simhei.ttf', False),
            ('/home/wtl/research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/wtl/research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/wtl/research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None
        detect_label_ids = torch.where(input_ids == label_ids, torch.tensor(0).to(input_ids.device), torch.tensor(1).to(input_ids.device))

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        # bert_hiddens [bsz,max_len,hid_dim]
        
        
        hiddens_detect = self.hiddens_gru(bert_hiddens)[0]
        detect_logits = self.detect_classifier(hiddens_detect)
        detect_logits = torch.sigmoid(detect_logits)


        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)

        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, bert_hiddens_mean), dim=-1)
        

        
        
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens

        #用hiddens来检测错误



        mask_embedding = torch.zeros_like(hiddens).to(hiddens.device)
        expanded_detect_logits = detect_logits[:,:,0].unsqueeze(-1).expand_as(hiddens)
        hiddens = hiddens * expanded_detect_logits + mask_embedding * (1-expanded_detect_logits)
        

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output += hiddens # residual connection

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)
        outputs = (logits,) + (hiddens,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:

            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)



            # Add detection errors here
            active_detect_loss = loss_mask.view(-1) == 1
            active_detect_logits = detect_logits.view(-1, 2)[active_detect_loss]
            active_detect_labels = detect_label_ids.view(-1)[active_detect_loss]
            detect_loss = loss_fct(active_detect_logits, active_detect_labels)

            total_loss = self.alpha * loss + (1-self.alpha) * detect_loss 
            outputs = (total_loss,) + outputs
        return outputs 


class SpellBertPho2ResArch3SoftMaskArch3Wubi(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch3SoftMaskArch3Wubi, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)


        self.hiddens_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=2,
            batch_first=True,
            dropout=0,
            bidirectional=True
        )
        self.detect_classifier = nn.Linear(config.hidden_size*2,2) #Correct and incorrect



        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.wubi_embeddings = nn.Embedding(wubi_convertor.get_wubi_size(),config.hidden_size,padding_idx=0)
        self.wubi_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        wubi_config = deepcopy(config)
        wubi_config.num_hidden_layers = 4
        self.wubi_model = BertModel(wubi_config)

        self.gate_net = nn.Linear(5*config.hidden_size, 4)




        out_config = deepcopy(config)
        out_config.num_hidden_layers = 6
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)
        self.detect_classifier = nn.Linear(2*config.hidden_size,2)
        self.alpha = 0.5
        self.beta = 1
        self.top_k = 5
        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/wtl/research/ReaLiSe/simhei.ttf', False),
            ('/home/wtl/research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/wtl/research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/wtl/research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        wubi_idx, wubi_lens = wubi_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        batch['wubi_idx'] = wubi_idx
        batch['wubi_lens'] = wubi_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        wubi_idx = batch['wubi_idx']
        wubi_lens = batch['wubi_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None
        detect_label_ids = torch.where(input_ids == label_ids, torch.tensor(0).to(input_ids.device), torch.tensor(1).to(input_ids.device))

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        # bert_hiddens [bsz,max_len,hid_dim]
        
        
        hiddens_detect = self.hiddens_gru(bert_hiddens)[0]
        detect_logits = self.detect_classifier(hiddens_detect)
        detect_logits = torch.sigmoid(detect_logits)


        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)


        wubi_embeddings = self.wubi_embeddings(wubi_idx)
        wubi_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=wubi_embeddings,
            lengths=wubi_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, wubi_hiddens = self.wubi_gru(wubi_embeddings)
        wubi_hiddens = wubi_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        wubi_hiddens = self.wubi_model(inputs_embeds=wubi_hiddens, attention_mask=attention_mask)[0]


        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, wubi_hiddens, bert_hiddens_mean), dim=-1)
        
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        g3 = torch.sigmoid(gated_values[:,:,3].unsqueeze(-1))
        
        # hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens + g3* wubi_hiddens
        hiddens = g0* bert_hiddens + g3* wubi_hiddens
        #用hiddens来检测错误

        mask_embedding = torch.zeros_like(hiddens).to(hiddens.device)
        expanded_detect_logits = detect_logits[:,:,0].unsqueeze(-1).expand_as(hiddens)
        hiddens = hiddens * expanded_detect_logits + mask_embedding * (1-expanded_detect_logits)
        

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output += hiddens # residual connection

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)
        outputs = (logits,) + (hiddens,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:

            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)

            # Add detection loss here
            active_detect_loss = loss_mask.view(-1) == 1
            active_detect_logits = detect_logits.view(-1, 2)[active_detect_loss]
            active_detect_labels = detect_label_ids.view(-1)[active_detect_loss]
            detect_loss = loss_fct(active_detect_logits, active_detect_labels)

            # Add contrastive loss here
            # active_logits: the prediction probability for all characters in vocabulary
            # pos_idx: the index of postive sample in vocabulary
            # K: the selected negative samples amount 
            # active_probability = torch.softmax(active_logits,dim=1)
            # seq_label = torch.arange(active_probability.shape[0])
            # pos_prob = active_probability[seq_label,active_labels]
            # neg_prob = torch.topk(active_probability,self.top_k)[0]
            # neg_idx = torch.topk(active_probability,self.top_k)[1]

            # # contrastive probability optimization objective
            # loss_list = []
            # for x in range(0,self.top_k):
            #     if neg_idx[:,x].tolist() != active_labels.tolist():
            #         loss_list.append(pos_prob-neg_prob[:,x])
            # cl_loss = -torch.stack(loss_list).mean() 
            cl_loss=0
            total_loss = self.alpha * loss + (1-self.alpha) * detect_loss + self.beta * cl_loss
            outputs = (total_loss,) + outputs
        return outputs 

class SpellBertPho2ResArch3SoftMaskArch3WubiContrast(BertPreTrainedModel):

    def __init__(self, config):
        super(SpellBertPho2ResArch3SoftMaskArch3WubiContrast, self).__init__(config)
        self.config = config

        self.vocab_size = config.vocab_size
        self.bert = BertModel(config)


        self.hiddens_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=2,
            batch_first=True,
            dropout=0,
            bidirectional=True
        )
        self.detect_classifier = nn.Linear(config.hidden_size*2,2) #Correct and incorrect



        self.pho_embeddings = nn.Embedding(pho2_convertor.get_pho_size(), config.hidden_size, padding_idx=0)
        self.pho_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        pho_config = deepcopy(config)
        pho_config.num_hidden_layers = 4
        self.pho_model = BertModel(pho_config)

        if self.config.num_fonts == 1:
            self.char_images = nn.Embedding(config.vocab_size, 1024)
            self.char_images.weight.requires_grad = False
        else:
            self.char_images_multifonts = torch.nn.Parameter(torch.rand(21128, self.config.num_fonts, 32, 32))
            self.char_images_multifonts.requires_grad = False

        if config.image_model_type == 0:
            self.resnet = CharResNet(in_channels=self.config.num_fonts)
        elif config.image_model_type == 1:
            self.resnet = CharResNet1()
        else:
            raise NotImplementedError('invalid image_model_type %d'%config.image_model_type)
        self.resnet_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)

        self.wubi_embeddings = nn.Embedding(wubi_convertor.get_wubi_size(),config.hidden_size,padding_idx=0)
        self.wubi_gru = nn.GRU(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=1,
            batch_first=True,
            dropout=0,
            bidirectional=False,
        )
        wubi_config = deepcopy(config)
        wubi_config.num_hidden_layers = 4
        self.wubi_model = BertModel(wubi_config)

        self.gate_net = nn.Linear(5*config.hidden_size, 4)




        out_config = deepcopy(config)
        out_config.num_hidden_layers = 6
        self.output_block = BertModel(out_config)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.vocab_size)
        self.detect_classifier = nn.Linear(2*config.hidden_size,2)
        self.alpha = 0.5
        self.beta = 1
        self.top_k = 5
        self.init_weights()

    def tie_cls_weight(self):
        self.classifier.weight = self.bert.embeddings.word_embeddings.weight

    def build_glyce_embed(self, vocab_dir, font_path, font_size=32):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path, 'r', encoding='utf-8') as f:
            vocab = [s.strip() for s in f]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) != 1 or (not _is_chinese_char(ord(char))):
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).reshape(char_images.shape[0], -1)
        assert char_images.shape == (21128, 1024)
        self.char_images.weight.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_multifonts(self, vocab_dir, num_fonts, use_traditional_font, font_size=32):
        font_paths = [
            ('/home/wtl/research/ReaLiSe/simhei.ttf', False),
            ('/home/wtl/research/ReaLiSe/xiaozhuan.ttf', False),
            ('/home/wtl/research/ReaLiSe/simhei.ttf', True),
        ]
        font_paths = font_paths[:num_fonts]
        if use_traditional_font:
            font_paths = font_paths[:-1]
            font_paths.append(('/home/wtl/research/ReaLiSe/simhei.ttf', True))
            self.converter = opencc.OpenCC('s2t.json')

        images_list = []
        for font_path, use_traditional in font_paths:
            images = self.build_glyce_embed_onefont(
                vocab_dir=vocab_dir,
                font_path=font_path,
                font_size=font_size,
                use_traditional=use_traditional,
            )
            images_list.append(images)

        char_images = torch.stack(images_list, dim=1).contiguous()
        self.char_images_multifonts.data.copy_(char_images)

    # Add by hengdaxu
    def build_glyce_embed_onefont(self, vocab_dir, font_path, font_size, use_traditional):
        vocab_path = os.path.join(vocab_dir, 'vocab.txt')
        with open(vocab_path) as f:
            vocab = [s.strip() for s in f.readlines()]
        if use_traditional:
            vocab = [self.converter.convert(c) if len(c) == 1 else c for c in vocab]

        font = ImageFont.truetype(font_path, size=font_size)

        char_images = []
        for char in vocab:
            if len(char) > 1:
                char_images.append(np.zeros((font_size, font_size)).astype(np.float32))
                continue
            image = font.getmask(char)
            image = np.asarray(image).astype(np.float32).reshape(image.size[::-1])  # Must be [::-1]

            # Crop
            image = image[:font_size, :font_size]

            # Pad
            if image.size != (font_size, font_size):
                back_image = np.zeros((font_size, font_size)).astype(np.float32)
                offset0 = (font_size - image.shape[0]) // 2
                offset1 = (font_size - image.shape[1]) // 2
                back_image[offset0:offset0 + image.shape[0], offset1:offset1 + image.shape[1]] = image
                image = back_image

            char_images.append(image)
        char_images = np.array(char_images)
        char_images = (char_images - np.mean(char_images)) / np.std(char_images)
        char_images = torch.from_numpy(char_images).contiguous()
        return char_images

    @staticmethod
    def build_batch(batch, tokenizer):
        src_idx = batch['src_idx'].flatten().tolist()
        chars = tokenizer.convert_ids_to_tokens(src_idx)
        pho_idx, pho_lens = pho2_convertor.convert(chars)
        wubi_idx, wubi_lens = wubi_convertor.convert(chars)
        batch['pho_idx'] = pho_idx
        batch['pho_lens'] = pho_lens
        batch['wubi_idx'] = wubi_idx
        batch['wubi_lens'] = wubi_lens
        return batch

    def forward(self, batch):
        input_ids = batch['src_idx']
        attention_mask = batch['masks']
        loss_mask = batch['loss_masks']
        pho_idx = batch['pho_idx']
        pho_lens = batch['pho_lens']
        wubi_idx = batch['wubi_idx']
        wubi_lens = batch['wubi_lens']
        label_ids = batch['tgt_idx'] if 'tgt_idx' in batch else None
        detect_label_ids = torch.where(input_ids == label_ids, torch.tensor(0).to(input_ids.device), torch.tensor(1).to(input_ids.device))

        input_shape = input_ids.size()

        bert_hiddens = self.bert(input_ids, attention_mask=attention_mask)[0]
        # bert_hiddens [bsz,max_len,hid_dim]
        
        
        hiddens_detect = self.hiddens_gru(bert_hiddens)[0]
        detect_logits = self.detect_classifier(hiddens_detect)
        detect_logits = torch.sigmoid(detect_logits)


        pho_embeddings = self.pho_embeddings(pho_idx)
        pho_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=pho_embeddings,
            lengths=pho_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, pho_hiddens = self.pho_gru(pho_embeddings)
        pho_hiddens = pho_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        pho_hiddens = self.pho_model(inputs_embeds=pho_hiddens, attention_mask=attention_mask)[0]

        src_idxs = input_ids.view(-1)

        if self.config.num_fonts == 1:
            images = self.char_images(src_idxs).reshape(src_idxs.shape[0], 1, 32, 32).contiguous()
        else:
            images = self.char_images_multifonts.index_select(dim=0, index=src_idxs)

        res_hiddens = self.resnet(images)
        res_hiddens = res_hiddens.reshape(input_shape[0], input_shape[1], -1).contiguous()
        res_hiddens = self.resnet_layernorm(res_hiddens)


        wubi_embeddings = self.wubi_embeddings(wubi_idx)
        wubi_embeddings = torch.nn.utils.rnn.pack_padded_sequence(
            input=wubi_embeddings,
            lengths=wubi_lens,
            batch_first=True,
            enforce_sorted=False,
        )
        _, wubi_hiddens = self.wubi_gru(wubi_embeddings)
        wubi_hiddens = wubi_hiddens.squeeze(0).reshape(input_shape[0], input_shape[1], -1).contiguous()
        wubi_hiddens = self.wubi_model(inputs_embeds=wubi_hiddens, attention_mask=attention_mask)[0]


        bert_hiddens_mean = (bert_hiddens * attention_mask.to(torch.float).unsqueeze(2)).sum(dim=1) / attention_mask.to(torch.float).sum(dim=1, keepdim=True)
        bert_hiddens_mean = bert_hiddens_mean.unsqueeze(1).expand(-1, bert_hiddens.size(1), -1)

        concated_outputs = torch.cat((bert_hiddens, pho_hiddens, res_hiddens, wubi_hiddens, bert_hiddens_mean), dim=-1)
        
        gated_values = self.gate_net(concated_outputs)
        # B * S * 3
        g0 = torch.sigmoid(gated_values[:,:,0].unsqueeze(-1))
        g1 = torch.sigmoid(gated_values[:,:,1].unsqueeze(-1))
        g2 = torch.sigmoid(gated_values[:,:,2].unsqueeze(-1))
        g3 = torch.sigmoid(gated_values[:,:,3].unsqueeze(-1))
        
        hiddens = g0* bert_hiddens + g1* pho_hiddens + g2* res_hiddens + g3* wubi_hiddens

        #用hiddens来检测错误

        mask_embedding = torch.zeros_like(hiddens).to(hiddens.device)
        expanded_detect_logits = detect_logits[:,:,0].unsqueeze(-1).expand_as(hiddens)
        hiddens = hiddens * expanded_detect_logits + mask_embedding * (1-expanded_detect_logits)
        

        outputs = self.output_block(inputs_embeds=hiddens,
                    position_ids=torch.zeros(input_ids.size(), dtype=torch.long, device=input_ids.device),
                    attention_mask=attention_mask)

        sequence_output = outputs[0]

        sequence_output += hiddens # residual connection

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)
        outputs = (logits,) + (hiddens,) + outputs[2:]  # add hidden states and attention if they are here
        if label_ids is not None:

            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            active_loss = loss_mask.view(-1) == 1
            active_logits = logits.view(-1, self.vocab_size)[active_loss]
            active_labels = label_ids.view(-1)[active_loss]
            loss = loss_fct(active_logits, active_labels)

            # Add detection loss here
            active_detect_loss = loss_mask.view(-1) == 1
            active_detect_logits = detect_logits.view(-1, 2)[active_detect_loss]
            active_detect_labels = detect_label_ids.view(-1)[active_detect_loss]
            detect_loss = loss_fct(active_detect_logits, active_detect_labels)

            seq_label = torch.arange(active_labels.shape[0])
            pos_prob = active_logits[seq_label, active_labels]
            neg_prob = torch.topk(active_logits, self.top_k)[0]
            neg_idx = torch.topk(active_logits, self.top_k)[1]


            # Contrastive probability optimization objective
            loss_list = []
            for x in range(self.top_k):
                loss_list.append(-torch.relu(neg_prob[:,x] - pos_prob))  # Use relu to set positive to 0 (when prob of gold label is greater than prob of prediction)
            cl_loss = -torch.stack(loss_list).mean()

            total_loss = self.alpha * loss + (1-self.alpha) * detect_loss + self.beta * cl_loss
            outputs = (total_loss,) + outputs
        return outputs 