from copy import deepcopy
from typing import List, Optional
import torch
from PIL import Image

from surya.schema import OrderBox, OrderResult
from surya.settings import settings
from tqdm import tqdm
import numpy as np


def get_batch_size():
    batch_size = settings.ORDER_BATCH_SIZE
    if batch_size is None:
        batch_size = 4
        if settings.TORCH_DEVICE_MODEL == "mps":
            batch_size = 4
        if settings.TORCH_DEVICE_MODEL == "cuda":
            batch_size = 32
    return batch_size


def rank_elements(arr):
    enumerated_and_sorted = sorted(enumerate(arr), key=lambda x: x[1])
    rank = [0] * len(arr)

    for rank_value, (original_index, value) in enumerate(enumerated_and_sorted):
        rank[original_index] = rank_value

    return rank


def batch_ordering(images: List, bboxes: List[List[List[float]]], model, processor, labels: Optional[List[List[str]]] = None) -> List[OrderResult]:
    assert all([isinstance(image, Image.Image) for image in images])
    assert len(images) == len(bboxes)
    batch_size = get_batch_size()

    if labels is not None:
        assert len(labels) == len(images)
        for l, b in zip(labels, bboxes):
            assert len(l) == len(b)

    images = [image.convert("RGB") for image in images]

    output_order = []
    for i in tqdm(range(0, len(images), batch_size), desc="Finding reading order"):
        batch_bboxes = deepcopy(bboxes[i:i+batch_size])
        batch_images = images[i:i+batch_size]
        orig_sizes = [image.size for image in batch_images]
        model_inputs = processor(images=batch_images, boxes=batch_bboxes)

        batch_pixel_values = model_inputs["pixel_values"]
        batch_bboxes = model_inputs["input_boxes"]
        batch_bbox_mask = model_inputs["input_boxes_mask"]
        batch_bbox_counts = model_inputs["input_boxes_counts"]

        batch_bboxes = torch.from_numpy(np.array(batch_bboxes, dtype=np.int32)).to(model.device)
        batch_bbox_mask = torch.from_numpy(np.array(batch_bbox_mask, dtype=np.int32)).to(model.device)
        batch_pixel_values = torch.tensor(np.array(batch_pixel_values), dtype=model.dtype).to(model.device)

        with torch.inference_mode():
            token_count = 0
            past_key_values = None
            encoder_outputs = None
            batch_predictions = [[] for _ in range(len(batch_images))]
            done = [False for _ in range(len(batch_images))]
            while token_count < settings.ORDER_MAX_BOXES:
                return_dict = model(
                    pixel_values=batch_pixel_values,
                    decoder_input_boxes=batch_bboxes,
                    decoder_input_boxes_mask=batch_bbox_mask,
                    decoder_input_boxes_counts=batch_bbox_counts,
                    encoder_outputs=encoder_outputs,
                    past_key_values=past_key_values,
                )
                logits = return_dict["logits"].detach().cpu()

                last_tokens = []
                last_token_mask = []
                for j, in range(logits.shape[0]):
                    new_logits = logits[j].clone()
                    new_logits[batch_predictions[j]] = -1e9 # Mask out already predicted tokens, we can only predict each token once
                    pred = int(torch.argmax(logits[j], dim=-1).item())

                    last_tokens.append([pred] * 4)
                    if pred == processor.token_pad_id:
                        last_token_mask.append([0])
                        done[j] = True
                    else:
                        last_token_mask.append([1])
                        batch_predictions[j].append(pred - processor.box_size["height"])  # Get rank prediction for given position

                # Break when we finished generating all sequences
                if all(done):
                    break

                past_key_values = return_dict["past_key_values"]
                encoder_outputs = (return_dict["encoder_last_hidden_state"],)

                batch_bboxes = torch.tensor(last_tokens, dtype=torch.long).to(model.device)
                batch_bbox_mask = torch.tensor(last_token_mask, dtype=torch.long).to(model.device)
                token_count += 1

        for j, row_pred in enumerate(batch_predictions):
            row_bboxes = bboxes[i+j]
            assert len(row_pred) == len(row_bboxes), "Mismatch between logits and bboxes."

            orig_size = orig_sizes[j]
            ranks = rank_elements(row_pred)

            if labels is not None:
                # This is to force headers/footers into the proper order
                row_label = labels[i+j]
                combined = [[i, bbox, label, rank] for i, (bbox, label, rank) in enumerate(zip(row_bboxes, row_label, ranks))]
                combined = sorted(combined, key=lambda x: x[3])

                sorted_boxes = ([row for row in combined if row[2] == "Page-header"] +
                                [row for row in combined if row[2] not in ["Page-header", "Page-footer"]] +
                                [row for row in combined if row[2] == "Page-footer"])

                # Re-rank after sorting
                for rank, row in enumerate(sorted_boxes):
                    row[3] = rank

                sorted_boxes = sorted(sorted_boxes, key=lambda x: x[0])
                row_bboxes = [row[1] for row in sorted_boxes]
                ranks = [row[3] for row in sorted_boxes]

            order_boxes = []
            for row_bbox, rank in zip(row_bboxes, ranks):
                order_box = OrderBox(
                    bbox=row_bbox,
                    position=rank,
                )
                order_boxes.append(order_box)

            result = OrderResult(
                bboxes=order_boxes,
                image_bbox=[0, 0, orig_size[0], orig_size[1]],
            )
            output_order.append(result)
    return output_order





