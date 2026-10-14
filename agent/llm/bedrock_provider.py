"""Amazon Bedrock provider (uses AWS credentials from aws configure / IAM role)."""

import os

from langchain_aws import ChatBedrockConverse


def build(max_tokens: int, model: str | None = None) -> ChatBedrockConverse:
    return ChatBedrockConverse(
        model=model or os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        max_tokens=max_tokens,
    )
