from tqdm import tqdm
import collections
import logging
import os
import json
import numpy as np

from ...tokenization_bert import BasicTokenizer, whitespace_tokenize
from .utils import DataProcessor, InputExample, InputFeatures
from ...file_utils import is_tf_available

if is_tf_available():
    import tensorflow as tf

logger = logging.getLogger(__name__)

def _improve_answer_span(doc_tokens, input_start, input_end, tokenizer,
                        orig_answer_text):
    """Returns tokenized answer spans that better match the annotated answer."""
    tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))

    for new_start in range(input_start, input_end + 1):
        for new_end in range(input_end, new_start - 1, -1):
            text_span = " ".join(doc_tokens[new_start:(new_end + 1)])
            if text_span == tok_answer_text:
                return (new_start, new_end)

    return (input_start, input_end)

def _check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span.start + doc_span.length - 1
        if position < doc_span.start:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span.start
        num_right_context = end - position
        score = min(num_left_context, num_right_context) + 0.01 * doc_span.length
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index

def _new_check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""
    # if len(doc_spans) == 1:
        # return True
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span["start"] + doc_span["length"] - 1
        if position < doc_span["start"]:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span["start"]
        num_right_context = end - position
        score = min(num_left_context, num_right_context) + 0.01 * doc_span["length"]
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index

def _is_whitespace(c):
    if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
        return True
    return False

def squad_convert_examples_to_features(examples, tokenizer, max_seq_length,
                                       doc_stride, max_query_length, is_training,
                                       sequence_a_is_doc=False):
    """Loads a data file into a list of `InputBatch`s."""

    # Defining helper methods    
    unique_id = 1000000000

    features = []
    for (example_index, example) in enumerate(tqdm(examples)):
        if is_training and not example.is_impossible:
            # Get start and end position
            start_position = example.start_position
            end_position = example.end_position

            # If the answer cannot be found in the text, then skip this example.
            actual_text = " ".join(example.doc_tokens[start_position:(end_position + 1)])
            cleaned_answer_text = " ".join(whitespace_tokenize(example.answer_text))
            if actual_text.find(cleaned_answer_text) == -1:
                logger.warning("Could not find answer: '%s' vs. '%s'", actual_text, cleaned_answer_text)
                continue


        tok_to_orig_index = []
        orig_to_tok_index = []
        all_doc_tokens = []
        for (i, token) in enumerate(example.doc_tokens):
            orig_to_tok_index.append(len(all_doc_tokens))
            sub_tokens = tokenizer.tokenize(token)
            for sub_token in sub_tokens:
                tok_to_orig_index.append(i)
                all_doc_tokens.append(sub_token)


        if is_training and not example.is_impossible:
            tok_start_position = orig_to_tok_index[example.start_position]
            if example.end_position < len(example.doc_tokens) - 1:
                tok_end_position = orig_to_tok_index[example.end_position + 1] - 1
            else:
                tok_end_position = len(all_doc_tokens) - 1

            (tok_start_position, tok_end_position) = _improve_answer_span(
                all_doc_tokens, tok_start_position, tok_end_position, tokenizer, example.answer_text
            )

        spans = []
        
        truncated_query = tokenizer.encode(example.question_text, add_special_tokens=False, max_length=max_query_length)
        sequence_added_tokens = tokenizer.max_len - tokenizer.max_len_single_sentence 
        sequence_pair_added_tokens = tokenizer.max_len - tokenizer.max_len_sentences_pair 

        span_doc_tokens = all_doc_tokens
        while len(spans) * doc_stride < len(all_doc_tokens):
            
            encoded_dict = tokenizer.encode_plus(
                truncated_query if not sequence_a_is_doc else span_doc_tokens, 
                span_doc_tokens if not sequence_a_is_doc else truncated_query, 
                max_length=max_seq_length, 
                return_overflowing_tokens=True, 
                padding_strategy='right',
                stride=max_seq_length - doc_stride - len(truncated_query) - sequence_pair_added_tokens,
                truncation_strategy='only_second' if not sequence_a_is_doc else 'only_first'
            )

            paragraph_len = min(len(all_doc_tokens) - len(spans) * doc_stride, max_seq_length - len(truncated_query) - sequence_pair_added_tokens)

            if tokenizer.pad_token_id in encoded_dict['input_ids']: 
                non_padded_ids = encoded_dict['input_ids'][:encoded_dict['input_ids'].index(tokenizer.pad_token_id)]
            else:
                non_padded_ids = encoded_dict['input_ids']

            tokens = tokenizer.convert_ids_to_tokens(non_padded_ids)

            token_to_orig_map = {}
            for i in range(paragraph_len):
                index = len(truncated_query) + sequence_added_tokens + i if not sequence_a_is_doc else i 
                token_to_orig_map[index] = tok_to_orig_index[len(spans) * doc_stride + i]

            encoded_dict["paragraph_len"] = paragraph_len
            encoded_dict["tokens"] = tokens
            encoded_dict["token_to_orig_map"] = token_to_orig_map
            encoded_dict["truncated_query_with_special_tokens_length"] = len(truncated_query) + sequence_added_tokens
            encoded_dict["token_is_max_context"] = {}
            encoded_dict["start"] = len(spans) * doc_stride
            encoded_dict["length"] = paragraph_len

            spans.append(encoded_dict)

            if "overflowing_tokens" not in encoded_dict:
                break
            span_doc_tokens = encoded_dict["overflowing_tokens"]

        for doc_span_index in range(len(spans)):
            for j in range(spans[doc_span_index]["paragraph_len"]):
                is_max_context = _new_check_is_max_context(spans, doc_span_index, doc_span_index * doc_stride + j)
                index = j if sequence_a_is_doc else spans[doc_span_index]["truncated_query_with_special_tokens_length"] + j
                spans[doc_span_index]["token_is_max_context"][index] = is_max_context

        for span in spans:
            # Identify the position of the CLS token
            cls_index = span['input_ids'].index(tokenizer.cls_token_id)

            # p_mask: mask with 1 for token than cannot be in the answer (0 for token which can be in an answer)
            # Original TF implem also keep the classification token (set to 0) (not sure why...)
            p_mask = np.array(span['token_type_ids'])

            p_mask = np.minimum(p_mask, 1)

            if not sequence_a_is_doc:
                # Limit positive values to one
                p_mask = 1 - p_mask

            p_mask[np.where(np.array(span["input_ids"]) == tokenizer.sep_token_id)[0]] = 1

            # Set the CLS index to '0'
            p_mask[cls_index] = 0


            span_is_impossible = example.is_impossible
            start_position = 0
            end_position = 0
            if is_training and not span_is_impossible:
                # For training, if our document chunk does not contain an annotation
                # we throw it out, since there is nothing to predict.
                doc_start = span["start"]
                doc_end = span["start"] + span["length"] - 1
                out_of_span = False

                if not (tok_start_position >= doc_start and tok_end_position <= doc_end):
                    out_of_span = True

                if out_of_span:
                    start_position = cls_index
                    end_position = cls_index
                    span_is_impossible = True
                else:
                    if sequence_a_is_doc:
                        doc_offset = 0
                    else:
                        doc_offset = len(truncated_query) + sequence_added_tokens
                        
                    start_position = tok_start_position - doc_start + doc_offset
                    end_position = tok_end_position - doc_start + doc_offset


            features.append(SquadFeatures(
                span['input_ids'],
                span['attention_mask'],
                span['token_type_ids'],
                cls_index,
                p_mask.tolist(),

                example_index=example_index,
                unique_id=unique_id,
                paragraph_len=span['paragraph_len'],
                token_is_max_context=span["token_is_max_context"],
                tokens=span["tokens"],
                token_to_orig_map=span["token_to_orig_map"],
                
                start_position=start_position,
                end_position=end_position
            ))

            unique_id += 1

    return features


class SquadProcessor(DataProcessor):
    """Processor for the SQuAD data set."""
    train_file = None
    dev_file = None

    def get_example_from_tensor_dict(self, tensor_dict):
        return SquadExample(
            tensor_dict['id'].numpy().decode("utf-8"),
            tensor_dict['question'].numpy().decode('utf-8'),
            tensor_dict['context'].numpy().decode('utf-8'),
            tensor_dict['answers']['text'][0].numpy().decode('utf-8'),
            tensor_dict['answers']['answer_start'][0].numpy(),
            tensor_dict['title'].numpy().decode('utf-8')
        )

    def get_examples_from_dataset(self, dataset):
        """See base class."""

        examples = []
        for tensor_dict in tqdm(dataset):
            examples.append(self.get_example_from_tensor_dict(tensor_dict)) 

        return examples

    def get_train_examples(self, data_dir, only_first=None):
        """See base class."""
        if self.train_file is None:
            raise ValueError("SquadProcessor should be instantiated via SquadV1Processor or SquadV2Processor")

        with open(os.path.join(data_dir, self.train_file), "r", encoding='utf-8') as reader:
            input_data = json.load(reader)["data"]
        return self._create_examples(input_data, "train", only_first)

    def get_dev_examples(self, data_dir, only_first=None):
        """See base class."""
        if self.dev_file is None:
            raise ValueError("SquadProcessor should be instantiated via SquadV1Processor or SquadV2Processor")
        
        with open(os.path.join(data_dir, self.dev_file), "r", encoding='utf-8') as reader:
            input_data = json.load(reader)["data"]
        return self._create_examples(input_data, "dev", only_first)

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def _create_examples(self, input_data, set_type, only_first=None):
        """Creates examples for the training and dev sets."""
        
        is_training = set_type == "train"
        examples = []
        for entry in tqdm(input_data):
            title = entry['title']
            for paragraph in entry["paragraphs"]:
                context_text = paragraph["context"]
                for qa in paragraph["qas"]:
                    qas_id = qa["id"]
                    question_text = qa["question"]
                    start_position_character = None
                    answer_text = None
                    
                    if "is_impossible" in qa:
                        is_impossible = qa["is_impossible"]
                    else:
                        is_impossible = False

                    if not is_impossible and is_training:
                        if (len(qa["answers"]) != 1):
                            raise ValueError(
                                "For training, each question should have exactly 1 answer.")
                        answer = qa["answers"][0]
                        answer_text = answer['text']
                        start_position_character = answer['answer_start']

                    example = SquadExample(
                        qas_id=qas_id,
                        question_text=question_text,
                        context_text=context_text,
                        answer_text=answer_text,
                        start_position_character=start_position_character,
                        title=title,
                        is_impossible=is_impossible
                    )

                    examples.append(example)

                    if only_first is not None and len(examples) > only_first:
                        return examples
        return examples

class SquadV1Processor(SquadProcessor):
    train_file = "train-v1.1.json"
    dev_file = "dev-v1.1.json"


class SquadV2Processor(SquadProcessor):
    train_file = "train-v2.0.json"
    dev_file = "dev-v2.0.json"
    

class SquadExample(object):
    """
    A single training/test example for the Squad dataset, as loaded from disk.
    """

    def __init__(self,
                 qas_id,
                 question_text,
                 context_text,
                 answer_text,
                 start_position_character,
                 title,
                 is_impossible=False):
        self.qas_id = qas_id
        self.question_text = question_text
        self.context_text = context_text
        self.answer_text = answer_text
        self.title = title
        self.is_impossible = is_impossible 

        self.start_position, self.end_position = 0, 0

        doc_tokens = []
        char_to_word_offset = []
        prev_is_whitespace = True

        # Split on whitespace so that different tokens may be attributed to their original position.
        for c in self.context_text:
            if _is_whitespace(c):
                prev_is_whitespace = True
            else:
                if prev_is_whitespace:
                    doc_tokens.append(c)
                else:
                    doc_tokens[-1] += c
                prev_is_whitespace = False
            char_to_word_offset.append(len(doc_tokens) - 1)

        self.doc_tokens = doc_tokens
        self.char_to_word_offset = char_to_word_offset

        # Start end end positions only has a value during evaluation.
        if start_position_character is not None and not is_impossible:
            self.start_position = char_to_word_offset[start_position_character]
            self.end_position = char_to_word_offset[start_position_character + len(answer_text) - 1]


class SquadFeatures(object):
    """
    Single squad example features to be fed to a model.
    Those features are model-specific.
    """

    def __init__(self,
                 input_ids,
                 attention_mask,
                 token_type_ids,
                 cls_index,
                 p_mask,
                 
                 example_index,
                 unique_id,
                 paragraph_len,
                 token_is_max_context,
                 tokens,
                 token_to_orig_map,

                 start_position,
                 end_position
        ):
        self.input_ids = input_ids 
        self.attention_mask = attention_mask
        self.token_type_ids = token_type_ids
        self.cls_index = cls_index
        self.p_mask = p_mask

        self.example_index = example_index
        self.unique_id = unique_id
        self.paragraph_len = paragraph_len
        self.token_is_max_context = token_is_max_context
        self.tokens = tokens
        self.token_to_orig_map = token_to_orig_map

        self.start_position = start_position
        self.end_position = end_position