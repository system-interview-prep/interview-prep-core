import math


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        raise ValueError("Embedding provider returned an empty vector")
    if len(left) != len(right):
        raise ValueError("CV and JD embedding dimensions do not match")

    dot_product = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("Embedding provider returned a zero vector")

    return max(0.0, min(1.0, dot_product / (left_norm * right_norm)))
