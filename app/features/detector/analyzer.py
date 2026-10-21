from typing import Dict, Any
from app.features.detector.bot_detector import bot_detector


class PostAnalyzer:
    @staticmethod
    def analyze_message(message) -> Dict[str, Any]:
        found = bot_detector.extract_bot(message)
        if found:
            return {
                'is_media_request': True,
                'bot_username': found['username'],
                'start_param': found.get('start_param'),
                'reason': 'bot_detected',
            }
        return {'is_media_request': False, 'bot_username': None, 'start_param': None,
                'reason': 'no_bot_pattern_found'}


analyzer = PostAnalyzer()
