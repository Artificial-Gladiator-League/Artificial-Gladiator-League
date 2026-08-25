def parse_thinking_seconds(request) -> float:
    """Read, parse, and clamp ai_thinking_seconds from POST data. Single source of truth for both apps."""
    try:
        seconds = float(request.POST.get("ai_thinking_seconds", "5"))
    except (ValueError, TypeError):
        seconds = 5.0
    return max(3.0, min(seconds, 15.0))
