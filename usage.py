"""Record provider-reported model usage for authenticated Nova accounts."""
import logging

import psycopg

from membership import connect

logger = logging.getLogger(__name__)


class MeteredResponses:
    def __init__(self, responses, user_id, feature):
        self.responses = responses
        self.user_id = user_id
        self.feature = feature

    def create(self, **kwargs):
        response = self.responses.create(**kwargs)
        if not self.user_id:
            return response

        usage = response.usage
        if usage is None:
            logger.warning("Nova AI usage unavailable: feature=%s", self.feature)
            return response

        search_calls = sum(item.type == "web_search_call" for item in response.output)
        try:
            with connect() as db:
                db.execute(
                    """
                    INSERT INTO nova_private.ai_usage (
                        response_id, user_id, feature, model, input_tokens,
                        cached_input_tokens, output_tokens, web_search_calls
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (response_id) DO NOTHING
                    """,
                    (
                        response.id, self.user_id, self.feature, response.model,
                        usage.input_tokens,
                        getattr(usage.input_tokens_details, "cached_tokens", 0) or 0,
                        usage.output_tokens, search_calls,
                    ),
                )
        except psycopg.Error as error:
            logger.warning("Nova AI usage write failed: feature=%s type=%s",
                           self.feature, type(error).__name__)
        return response
