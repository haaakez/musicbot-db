"""Radio eligibility and weighting, independent of Discord and Lavalink."""


def eligible(ratings, listeners, threshold, ignore_unrated):
    for user_id in listeners:
        score = ratings.get(user_id)
        if score is None:
            if not ignore_unrated:
                return False
        elif score == -1 or score < threshold:
            return False
    return True


def weight(play_count, ratings):
    scores = [score for score in ratings.values() if score >= 0]
    average = sum(scores) / len(scores) if scores else 0
    return 100 / ((play_count or 0) + 1) + average * 10
