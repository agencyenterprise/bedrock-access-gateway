import base64
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import AsyncIterable, Iterable, Literal, Dict, Any

import boto3
import numpy as np
import requests
import tiktoken
from fastapi import HTTPException


from api.models.base import BaseChatModel, BaseEmbeddingsModel
from api.schema import (
    # Chat
    ChatResponse,
    ChatRequest,
    Choice,
    ChatResponseMessage,
    Usage,
    ChatStreamResponse,
    ChoiceDelta,
    ImageContent,
    TextContent,
    ResponseFunction,
    ToolCall,
    # Embeddings
    EmbeddingsRequest,
    EmbeddingsResponse,
    EmbeddingsUsage,
    Embedding,
)
from api.setting import DEBUG, AWS_REGION

logger = logging.getLogger(__name__)

bedrock_runtime = boto3.client(
    service_name="bedrock-runtime",
    region_name=AWS_REGION,
)

SUPPORTED_BEDROCK_MODELS = {
    "anthropic.claude-instant-v1": "Claude Instant",
    "anthropic.claude-v2:1": "Claude",
    "anthropic.claude-v2": "Claude",
    "anthropic.claude-3-sonnet-20240229-v1:0": "Claude 3 Sonnet",
    "anthropic.claude-3-opus-20240229-v1:0": "Claude 3 Opus",
    "anthropic.claude-3-haiku-20240307-v1:0": "Claude 3 Haiku",
    "meta.llama2-13b-chat-v1": "Llama 2 Chat 13B",
    "meta.llama2-70b-chat-v1": "Llama 2 Chat 70B",
    "meta.llama3-8b-instruct-v1:0": "Llama 3 8B Instruct",
    "meta.llama3-70b-instruct-v1:0": "Llama 3 70B Instruct",
    "mistral.mistral-7b-instruct-v0:2": "Mistral 7B Instruct",
    "mistral.mixtral-8x7b-instruct-v0:1": "Mixtral 8x7B Instruct",
    "mistral.mistral-large-2402-v1:0": "Mistral Large",
    "cohere.command-r-v1:0": "Command R",
    "cohere.command-r-plus-v1:0": "Command R+",
}

SUPPORTED_BEDROCK_EMBEDDING_MODELS = {
    "cohere.embed-multilingual-v3": "Cohere Embed Multilingual",
    "cohere.embed-english-v3": "Cohere Embed English",
    # Disable Titan embedding.
    # "amazon.titan-embed-text-v1": "Titan Embeddings G1 - Text",
    # "amazon.titan-embed-image-v1": "Titan Multimodal Embeddings G1"
}

ENCODER = tiktoken.get_encoding("cl100k_base")


# https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters.html
class BedrockModel(BaseChatModel, ABC):
    accept = "application/json"
    content_type = "application/json"

    # Default field name to get the response message
    text_field_name = "text"

    # Default field name to get the response finish reason
    finish_reason_field_name = "finish_reason"

    @abstractmethod
    def compose_request_body(self, chat_request: ChatRequest) -> str:
        """Since the request body to Bedrock varies,
        each model should implement this to compose the request body.

        :param chat_request:
        :return: request body as a string
        """
        raise NotImplementedError()

    def chat(self, chat_request: ChatRequest) -> ChatResponse:
        """Default implementation for Chat API."""
        if DEBUG:
            logger.info("Raw request: " + chat_request.model_dump_json())
        request_body = self.compose_request_body(chat_request)

        if DEBUG:
            logger.info("Bedrock request: " + request_body)

        response = self.invoke_model(
            request_body=request_body,
            model_id=chat_request.model,
        )
        message_id = self.generate_message_id()
        return self.parse_response(chat_request, response, message_id)

    def chat_stream(self, chat_request: ChatRequest) -> AsyncIterable[bytes]:
        """Default implementation for Chat Stream API"""
        if DEBUG:
            logger.info("Raw request: " + chat_request.model_dump_json())
        request_body = self.compose_request_body(chat_request)
        response = self.invoke_model(
            request_body=request_body,
            model_id=chat_request.model,
            with_stream=True,
        )

        message_id = self.generate_message_id()
        for stream_response in self.parse_stream_response(
            chat_request, response, message_id
        ):
            if stream_response.choices:
                yield self.stream_response_to_bytes(stream_response)
            elif (
                chat_request.stream_options
                and chat_request.stream_options.include_usage
            ):
                # An empty choices for Usage as per OpenAI doc below:
                # if you set stream_options: {"include_usage": true}.
                # an additional chunk will be streamed before the data: [DONE] message.
                # The usage field on this chunk shows the token usage statistics for the entire request,
                # and the choices field will always be an empty array.
                # All other chunks will also include a usage field, but with a null value.
                yield self.stream_response_to_bytes(stream_response)
        # return an [DONE] message at the end.
        yield self.stream_response_to_bytes()

    def get_message_text(self, response_body: dict) -> str | None:
        """Default func to get the response message.

        Ideally, only the field name should be changed."""
        return response_body.get(self.text_field_name)

    def get_message_finish_reason(self, response_body: dict) -> str | None:
        """Default func to get the finish message.

        Ideally, only the field name should be changed."""
        return response_body.get(self.finish_reason_field_name)

    def get_message_usage(self, response_body: dict) -> tuple[int, int]:
        """Default func to get the finish message.

        Can be overridden in the detail model for complex cases."""
        input_tokens = int(response_body.get("prompt_token_count", "0"))
        output_tokens = int(response_body.get("generation_token_count", "0"))
        return input_tokens, output_tokens

    def parse_response(
        self, chat_request: ChatRequest, service_response: dict, message_id: str
    ) -> ChatResponse:
        response_body = json.loads(service_response.get("body").read())
        if DEBUG:
            logger.info("Bedrock response body: " + str(response_body))

        input_tokens, output_tokens = self.get_message_usage(response_body)
        return self.create_response(
            model=chat_request.model,
            message_id=message_id,
            message=self.get_message_text(response_body),
            finish_reason=self.get_message_finish_reason(response_body),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def parse_stream_response(
        self, chat_request: ChatRequest, service_response: dict, message_id: str
    ) -> Iterable[ChatStreamResponse]:

        chunk_id = 0
        for event in service_response.get("body"):
            if DEBUG:
                logger.info("Bedrock response chunk: " + str(event))
            chunk = json.loads(event["chunk"]["bytes"])
            chunk_id += 1

            response = self.create_response_stream(
                model=chat_request.model,
                message_id=message_id,
                chunk_message=self.get_message_text(chunk),
                finish_reason=self.get_message_finish_reason(chunk),
            )
            yield response
            # Get the usage for streaming response anyway.
            if "amazon-bedrock-invocationMetrics" in chunk:
                yield self.create_response_stream(
                    model=chat_request.model,
                    message_id=message_id,
                    input_tokens=chunk["amazon-bedrock-invocationMetrics"][
                        "inputTokenCount"
                    ],
                    output_tokens=chunk["amazon-bedrock-invocationMetrics"][
                        "outputTokenCount"
                    ],
                )

    def invoke_model(self, request_body: str, model_id: str, with_stream: bool = False):
        if DEBUG:
            logger.info("Invoke Bedrock Model: " + model_id)
            logger.info("Bedrock request body: " + request_body)
        try:
            if with_stream:
                return bedrock_runtime.invoke_model_with_response_stream(
                    body=request_body,
                    modelId=model_id,
                    accept=self.accept,
                    contentType=self.content_type,
                )
            return bedrock_runtime.invoke_model(
                body=request_body,
                modelId=model_id,
                accept=self.accept,
                contentType=self.content_type,
            )
        except bedrock_runtime.exceptions.ValidationException as e:
            logger.error("Validation Error: " + str(e))
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(e)
            raise HTTPException(status_code=500, detail=str(e))

    @staticmethod
    def merge_message(messages: list[dict]) -> list[dict]:
        """Merge the request messages with the same role as previous message"""
        merged_messages = []
        prev_role = None
        merged_content = ""

        for message in messages:
            role = message["role"]
            content = message["content"]
            if role != prev_role or isinstance(content, list):
                if prev_role:
                    merged_messages.append(
                        {"role": prev_role, "content": merged_content}
                    )
                if isinstance(content, str):
                    merged_content = content
                    prev_role = role
                else:
                    merged_messages.append({"role": role, "content": content})
                    prev_role = None
                    merged_content = ""
            else:
                if content == merged_content:
                    # ignore duplicates
                    continue
                merged_content += "\n" + content

        if merged_content:
            merged_messages.append({"role": prev_role, "content": merged_content})
        return merged_messages

    @staticmethod
    def create_response(
        model: str,
        message_id: str,
        message: str | None = None,
        finish_reason: str | None = None,
        tools: list[ToolCall] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> ChatResponse:
        response = ChatResponse(
            id=message_id,
            model=model,
            choices=[
                Choice(
                    index=0,
                    message=ChatResponseMessage(
                        role="assistant",
                        tool_calls=tools,
                        content=message,
                    ),
                    finish_reason="tool_calls" if tools else finish_reason,
                )
            ],
            usage=Usage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
        if DEBUG:
            logger.info("Proxy response :" + response.model_dump_json())
        return response

    @staticmethod
    def create_response_stream(
        model: str,
        message_id: str,
        chunk_message: str | None = None,
        finish_reason: str | None = None,
        tools: list[ToolCall] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> ChatStreamResponse:
        if chunk_message or finish_reason or tools:
            response = ChatStreamResponse(
                id=message_id,
                model=model,
                choices=[
                    ChoiceDelta(
                        index=0,
                        delta=ChatResponseMessage(
                            role="assistant",
                            tool_calls=tools,
                            content=chunk_message,
                        ),
                        finish_reason=finish_reason,
                    )
                ],
            )
        else:
            response = ChatStreamResponse(
                id=message_id,
                model=model,
                choices=[],
                usage=Usage(
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                ),
            )
        if DEBUG:
            logger.info("Proxy response :" + response.model_dump_json())
        return response


class ClaudeModel(BedrockModel):
    anthropic_version = "bedrock-2023-05-31"
    # follow these instructions for tool uses:
    tool_prompt = """You have access to the following tools:
{tools}

Please think if you need to use a tool or not for user's question, you must:
1. Respond Y or N within <tool></tool> tags first to indicate that.
2. If a tool is needed, MUST respond a JSON object matching the following schema within <function></function> tags:
   {{"name": $TOOL_NAME, "arguments": {{"$PARAMETER_NAME": "$PARAMETER_VALUE", ...}}}}
3. If no tools is needed, respond with normal text."""

    def compose_request_body(self, chat_request: ChatRequest) -> str:
        args = {
            "anthropic_version": self.anthropic_version,
            "max_tokens": chat_request.max_tokens,
            "top_p": chat_request.top_p,
            "temperature": chat_request.temperature,
        }
        system_prompt = ""
        converted_messages = []
        for message in chat_request.messages:
            if message.role == "system":
                assert isinstance(message.content, str)
                system_prompt += message.content + "\n"
            elif message.role == "user" and not isinstance(message.content, str):
                converted_messages.append(
                    {
                        "role": message.role,
                        "content": self._parse_content_parts(message.content),
                    }
                )
            elif message.role == "assistant" and not message.content:
                # if content is empty
                # create the content using the tool call info.
                tool_content = "[Tool use for `{}` with id `{}` with the following `input`]\n{}".format(
                    message.tool_calls[0].function.name,
                    message.tool_calls[0].id,
                    message.tool_calls[0].function.arguments,
                )
                converted_messages.append(
                    {"role": message.role, "content": tool_content}
                )
            elif message.role == "tool":
                # Since bedrock does not support tool role
                # Convert the tool message to a user message.
                converted_messages.append(
                    {
                        "role": "user",
                        "content": "[Tool result with matching id `{}` of `{}`] ".format(
                            message.tool_call_id, message.content
                        ),
                    }
                )
            else:
                converted_messages.append(
                    {"role": message.role, "content": message.content}
                )

        if chat_request.tools:
            tools_str = json.dumps(
                [tool.function.model_dump() for tool in chat_request.tools]
            )
            system_prompt += self.tool_prompt.format(tools=tools_str)
            converted_messages.append({"role": "assistant", "content": "<tool>"})
            args["stop_sequences"] = ["</function>"]
        args["messages"] = self.merge_message(converted_messages)
        if system_prompt:
            if DEBUG:
                logger.info("System Prompt: " + system_prompt)
            args["system"] = system_prompt

        return json.dumps(args)

    def parse_response(
        self, chat_request: ChatRequest, service_response: dict, message_id: str
    ) -> ChatResponse:
        response_body = json.loads(service_response.get("body").read())
        if DEBUG:
            logger.info("Bedrock response body: " + str(response_body))
        message = response_body["content"][0]["text"]
        finish_reason = response_body["stop_reason"]
        tools = None
        if chat_request.tools:
            if message.startswith("Y</tool>"):
                tools = self._parse_tool_message(message)
                message = None
                finish_reason = "tool_calls"
            elif message.startswith("N</tool>"):
                message = message[8:].lstrip("\n")
        return self.create_response(
            model=chat_request.model,
            message_id=message_id,
            message=message,
            tools=tools,
            finish_reason=finish_reason,
            input_tokens=response_body["usage"]["input_tokens"],
            output_tokens=response_body["usage"]["output_tokens"],
        )

    def parse_stream_response(
        self, chat_request: ChatRequest, service_response: dict, message_id: str
    ) -> Iterable[ChatStreamResponse]:

        chunk_id = 0
        tool_message = ""
        first_token = True
        index = 0
        for event in service_response.get("body"):
            if DEBUG:
                logger.info("Bedrock response chunk: " + str(event))
            chunk = json.loads(event["chunk"]["bytes"])
            chunk_id += 1

            if chunk["type"] == "message_stop":
                # Get the usage for streaming response anyway.
                if "amazon-bedrock-invocationMetrics" in chunk:
                    yield self.create_response_stream(
                        model=chat_request.model,
                        message_id=message_id,
                        input_tokens=chunk["amazon-bedrock-invocationMetrics"][
                            "inputTokenCount"
                        ],
                        output_tokens=chunk["amazon-bedrock-invocationMetrics"][
                            "outputTokenCount"
                        ],
                    )
                break

            elif chunk["type"] == "message_delta":
                chunk_message = ""
                finish_reason = chunk["delta"]["stop_reason"]

                # Send tool message first if any.
                if chat_request.tools and tool_message:
                    tools = self._parse_tool_message(tool_message)
                    finish_reason = "tool_calls"
                    response = self.create_response_stream(
                        model=chat_request.model,
                        message_id=message_id,
                        tools=tools,
                    )
                    yield response

            elif chunk["type"] == "content_block_delta":
                chunk_message = chunk["delta"]["text"]
                finish_reason = None
                if chat_request.tools:
                    # Check first token
                    if not tool_message and chunk_message == "Y":
                        tool_message = "Y"
                        continue
                    if tool_message:
                        # Buffer all chunk message
                        # in order to extract tool call info
                        tool_message += chunk_message
                        continue
                    if index < 3:
                        # Ignore the N</tool>, which is 3 tokens
                        index += 1
                        continue
                    if first_token:
                        chunk_message = chunk_message.lstrip("\n")
                        first_token = False
            else:
                continue
            response = self.create_response_stream(
                model=chat_request.model,
                message_id=message_id,
                chunk_message=chunk_message,
                finish_reason=finish_reason,
            )

            yield response

    def _parse_tool_message(self, tool_message: str) -> list[ToolCall]:
        if DEBUG:
            logger.info("Tool message: " + tool_message.replace("\n", " "))
        try:
            tool_messages = tool_message[
                tool_message.rindex("<function>") + len("<function>") :
            ]
            function = json.loads(tool_messages.replace("\n", " "))
            args = json.dumps(function.get("arguments", {}))
            function = ResponseFunction(name=function["name"], arguments=args)

            return [
                ToolCall(
                    # id="0",
                    function=function,
                )
            ]

        except Exception as e:
            logger.error("Failed to parse tool response" + str(e))
            raise HTTPException(status_code=500, detail="Failed to parse tool response")

    def _get_base64_image(self, image_url: str) -> tuple[str, str]:
        """Try to get the base64 data from an image url.

        returns a tuple of (Image Data, Content Type)
        """
        pattern = r"^data:(image/[a-z]*);base64,\s*"
        content_type = re.search(pattern, image_url)
        # if already base64 encoded.
        # Only supports 'image/jpeg', 'image/png', 'image/gif' or 'image/webp'
        if content_type:
            image_data = re.sub(pattern, "", image_url)
            return image_data, content_type.group(1)

        # Send a request to the image URL
        response = requests.get(image_url)
        # Check if the request was successful
        if response.status_code == 200:

            content_type = response.headers.get("Content-Type")
            if not content_type.startswith("image"):
                content_type = "image/jpeg"
            # Get the image content
            image_content = response.content
            # Encode the image content as base64
            base64_image = base64.b64encode(image_content)
            return base64_image.decode("utf-8"), content_type
        else:
            raise HTTPException(
                status_code=500, detail="Unable to access the image url"
            )

    def _parse_content_parts(
        self, content: list[TextContent | ImageContent]
    ) -> list[dict]:
        # See: https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html
        content_parts = []
        for part in content:
            if isinstance(part, TextContent):
                content_parts.append(part.model_dump())
            else:
                image_data, content_type = self._get_base64_image(part.image_url.url)
                content_parts.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": content_type,
                            "data": image_data,
                        },
                    }
                )
        return content_parts


class CustomImportModel(BedrockModel):
    text_field_name = "generation"
    finish_reason_field_name = "stop_reason"

    @staticmethod
    def create_prompt(chat_request: ChatRequest) -> str:
        """Create a prompt message for the custom imported model."""
        prompt_lines = []
        for msg in chat_request.messages:
            prompt_lines.append(f"<|{msg.role}|>{msg.content}</s>")
        prompt_lines.append("<|assistant|>")
        return "".join(prompt_lines)

    def compose_request_body(self, chat_request: ChatRequest) -> str:
        prompt = self.create_prompt(chat_request)
        args = {
            "prompt": prompt,
            "max_tokens": chat_request.max_tokens or 512,  # Default value
            "temperature": chat_request.temperature or 0.5,  # Default value
            "top_p": chat_request.top_p or 0.9,  # Default value
            "top_k": 200,  # Default value
            "stop": [],  # Default value
        }
        return json.dumps(args)

    def get_finish_reason(self, response: Dict[str, Any]) -> str:
        """Get the finish reason from the response."""
        if self.finish_reason_field_name in response:
            return response[self.finish_reason_field_name]
        else:
            return "unknown"

    def get_message_text(self, response_body: dict) -> str:
        return response_body["generation"]

    def get_message_finish_reason(self, response_body: dict) -> str:
        return response_body["stop_reason"]

    def get_message_usage(self, response_body: dict) -> tuple[int, int]:
        input_tokens = int(response_body.get("prompt_token_count", "0"))
        output_tokens = int(response_body.get("generation_token_count", "0"))
        return input_tokens, output_tokens


class LlamaModel(BedrockModel):
    text_field_name = "generation"
    finish_reason_field_name = "stop_reason"

    @staticmethod
    def create_llama3_prompt(chat_request: ChatRequest) -> str:
        """Create a prompt message for Llama 3 following below example:

        <|begin_of_text|><|start_header_id|>system<|end_header_id|>

        {{ system_prompt }}<|eot_id|><|start_header_id|>user<|end_header_id|>

        {{ user_message_1 }}<|eot_id|><|start_header_id|>assistant<|end_header_id|>

        {{ model_answer_1 }}<|eot_id|><|start_header_id|>user<|end_header_id|>

        {{ user_message_2 }}<|eot_id|><|start_header_id|>assistant<|end_header_id|>
        """
        if DEBUG:
            logger.info("Convert below messages to prompt for Llama 3: ")
            for msg in chat_request.messages:
                logger.info(msg.model_dump_json())
        bos_token = "<|begin_of_text|>"

        prompt_lines = []
        for msg in chat_request.messages:
            prompt_lines.append(
                f"<|start_header_id|>{msg.role}<|end_header_id|>\n\n{msg.content}<|eot_id|>"
            )
        prompt_lines.append(f"<|start_header_id|>assistant<|end_header_id|>\n\n")
        prompt = bos_token + "".join(prompt_lines)
        if DEBUG:
            logger.info("Converted prompt: " + prompt.replace("\n", "\\n"))
        return prompt

    @staticmethod
    def create_llama2_prompt(chat_request: ChatRequest) -> str:
        """Create a prompt message for Llama 2 following below example:

        <s>[INST] <<SYS>>\n{your_system_message}\n<</SYS>>\n\n{user_message_1} [/INST] {model_reply_1}</s>
        <s>[INST] {user_message_2} [/INST]
        """
        if DEBUG:
            logger.info("Convert below messages to prompt for Llama 2: ")
            for msg in chat_request.messages:
                logger.info(msg.model_dump_json())
        bos_token = "<s>"
        eos_token = "</s>"
        prompt = ""
        end_turn = False
        system_prompt = ""
        for msg in chat_request.messages:
            if msg.role == "system":
                system_prompt += "\n" + msg.content + "\n"
                continue
            if msg.role == "tool":
                raise HTTPException(
                    status_code=500,
                    detail="Tool prompt is not supported for Llama 2 model",
                )
            if not isinstance(msg.content, str):
                raise HTTPException(
                    status_code=400, detail="Content must be a string for Llama 2 model"
                )
            if msg.role == "user":
                if end_turn:
                    prompt += bos_token + "[INST] "
                prompt += msg.content + " [/INST] "
                end_turn = False
            else:
                prompt += msg.content + eos_token
                end_turn = True

        if system_prompt:
            system_prompt = "<<SYS>>" + system_prompt + "<</SYS>>"
        prompt = bos_token + "[INST] " + system_prompt + prompt
        if DEBUG:
            logger.info("Converted prompt: " + prompt.replace("\n", "\\n"))
        return prompt

    def compose_request_body(self, chat_request: ChatRequest) -> str:
        if chat_request.model.startswith("meta.llama2"):
            prompt = self.create_llama2_prompt(chat_request)
        else:
            prompt = self.create_llama3_prompt(chat_request)
        args = {
            "prompt": prompt,
            "max_gen_len": chat_request.max_tokens,
            "temperature": chat_request.temperature,
            "top_p": chat_request.top_p,
        }
        return json.dumps(args)


class MistralModel(BedrockModel):
    text_field_name = "text"
    finish_reason_field_name = "stop_reason"

    def _convert_prompt(self, chat_request: ChatRequest) -> str:
        """Create a prompt message follow below example:

        <s>[INST] {your_system_message}\n{user_message_1} [/INST] {model_reply_1}</s>
        <s>[INST] {user_message_2} [/INST]
        """
        # TODO: maybe reuse the Llama 2 one.
        if DEBUG:
            logger.info("Convert below messages to prompt for Mistral/Mixtral model: ")
            for msg in chat_request.messages:
                logger.info(msg.model_dump_json())
        bos_token = "<s>"
        eos_token = "</s>"
        prompt = ""
        end_turn = False
        system_prompt = ""
        for msg in chat_request.messages:
            if msg.role == "system":
                system_prompt += "\n" + msg.content + "\n"
                continue
            if msg.role == "tool":
                raise HTTPException(
                    status_code=500,
                    detail="Tool prompt is not supported for Mistral/Mixtral model",
                )
            if not isinstance(msg.content, str):
                raise HTTPException(
                    status_code=400,
                    detail="Content must be a string for Mistral/Mixtral model",
                )
            if msg.role == "user":
                if end_turn:
                    prompt += bos_token + "[INST] "
                prompt += msg.content + " [/INST] "
                end_turn = False
            else:
                prompt += msg.content + eos_token
                end_turn = True

        prompt = bos_token + "[INST] " + system_prompt + prompt
        if DEBUG:
            logger.info("Converted prompt: " + prompt.replace("\n", "\\n"))
        return prompt

    def compose_request_body(self, chat_request: ChatRequest) -> str:
        prompt = self._convert_prompt(chat_request)
        args = {
            "prompt": prompt,
            "max_tokens": chat_request.max_tokens,
            "temperature": chat_request.temperature,
            "top_p": chat_request.top_p,
        }
        return json.dumps(args)

    def get_message_text(self, response_body: dict) -> str | None:
        return super().get_message_text(response_body["outputs"][0])

    def get_message_finish_reason(self, response_body: dict) -> str | None:
        return super().get_message_finish_reason(response_body["outputs"][0])

    def get_message_usage(self, response_body: dict) -> tuple[int, int]:
        # Mistral/Mixtral does not provide info about usage
        return 0, 0


class CohereCommandModel(BedrockModel):

    def _parse_message(self, message) -> dict:
        if message.role not in ["user", "assistant"]:
            raise HTTPException(
                status_code=400, detail="Only user or assistant message is supported"
            )
        return {
            "role": "USER" if message.role == "user" else "CHATBOT",
            "message": message.content,
        }

    def compose_request_body(self, chat_request: ChatRequest) -> str:
        messages = chat_request.messages
        if messages[-1].role != "user":
            raise HTTPException(
                status_code=400, detail="Last message should be a valid user message"
            )
        chat_history = [self._parse_message(msg) for msg in messages[:-1]]
        args = {
            "message": messages[-1].content,
            "chat_history": chat_history,
            "max_tokens": chat_request.max_tokens,
            "temperature": chat_request.temperature,
            "p": chat_request.top_p,
        }
        return json.dumps(args)


class BedrockEmbeddingsModel(BaseEmbeddingsModel, ABC):
    accept = "application/json"
    content_type = "application/json"

    def _invoke_model(self, args: dict, model_id: str):
        body = json.dumps(args)
        if DEBUG:
            logger.info("Invoke Bedrock Model: " + model_id)
            logger.info("Bedrock request body: " + body)
        try:
            return bedrock_runtime.invoke_model(
                body=body,
                modelId=model_id,
                accept=self.accept,
                contentType=self.content_type,
            )
        except bedrock_runtime.exceptions.ValidationException as e:
            logger.error("Validation Error: " + str(e))
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(e)
            raise HTTPException(status_code=500, detail=str(e))

    def _create_response(
        self,
        embeddings: list[float],
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        encoding_format: Literal["float", "base64"] = "float",
    ) -> EmbeddingsResponse:
        data = []
        for i, embedding in enumerate(embeddings):
            if encoding_format == "base64":
                arr = np.array(embedding, dtype=np.float32)
                arr_bytes = arr.tobytes()
                encoded_embedding = base64.b64encode(arr_bytes)
                data.append(Embedding(index=i, embedding=encoded_embedding))
            else:
                data.append(Embedding(index=i, embedding=embedding))
        response = EmbeddingsResponse(
            data=data,
            model=model,
            usage=EmbeddingsUsage(
                prompt_tokens=input_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
        if DEBUG:
            logger.info("Proxy response :" + response.model_dump_json())
        return response


class CohereEmbeddingsModel(BedrockEmbeddingsModel):

    def _parse_args(self, embeddings_request: EmbeddingsRequest) -> dict:
        texts = []
        if isinstance(embeddings_request.input, str):
            texts = [embeddings_request.input]
        elif isinstance(embeddings_request.input, list):
            texts = embeddings_request.input
        elif isinstance(embeddings_request.input, Iterable):
            # For encoded input
            # The workaround is to use tiktoken to decode to get the original text.
            encodings = []
            for inner in embeddings_request.input:
                if isinstance(inner, int):
                    # Iterable[int]
                    encodings.append(inner)
                else:
                    # Iterable[Iterable[int]]
                    text = ENCODER.decode(list(inner))
                    texts.append(text)
            if encodings:
                texts.append(ENCODER.decode(encodings))

        # Maximum of 2048 characters
        args = {
            "texts": texts,
            "input_type": "search_document",
            "truncate": "END",  # "NONE|START|END"
        }
        return args

    def embed(self, embeddings_request: EmbeddingsRequest) -> EmbeddingsResponse:
        response = self._invoke_model(
            args=self._parse_args(embeddings_request), model_id=embeddings_request.model
        )
        response_body = json.loads(response.get("body").read())
        if DEBUG:
            logger.info("Bedrock response body: " + str(response_body))

        return self._create_response(
            embeddings=response_body["embeddings"],
            model=embeddings_request.model,
            encoding_format=embeddings_request.encoding_format,
        )


class TitanEmbeddingsModel(BedrockEmbeddingsModel):

    def _parse_args(self, embeddings_request: EmbeddingsRequest) -> dict:
        if isinstance(embeddings_request.input, str):
            input_text = embeddings_request.input
        elif (
            isinstance(embeddings_request.input, list)
            and len(embeddings_request.input) == 1
        ):
            input_text = embeddings_request.input[0]
        else:
            raise ValueError(
                "Amazon Titan Embeddings models support only single strings as input."
            )
        args = {
            "inputText": input_text,
            # Note: inputImage is not supported!
        }
        if embeddings_request.model == "amazon.titan-embed-image-v1":
            args["embeddingConfig"] = (
                embeddings_request.embedding_config
                if embeddings_request.embedding_config
                else {"outputEmbeddingLength": 1024}
            )
        return args

    def embed(self, embeddings_request: EmbeddingsRequest) -> EmbeddingsResponse:
        response = self._invoke_model(
            args=self._parse_args(embeddings_request), model_id=embeddings_request.model
        )
        response_body = json.loads(response.get("body").read())
        if DEBUG:
            logger.info("Bedrock response body: " + str(response_body))

        return self._create_response(
            embeddings=[response_body["embedding"]],
            model=embeddings_request.model,
            input_tokens=response_body["inputTextTokenCount"],
        )


def get_model(model_id: str) -> BedrockModel:
    if DEBUG:
        logger.info("model id is " + model_id)
    if "imported-model" in model_id:
        return CustomImportModel()
    if model_id.startswith("anthropic.claude"):
        return ClaudeModel()
    elif model_id.startswith("meta.llama"):
        return LlamaModel()
    elif model_id.startswith("mistral.mistral") or model_id.startswith(
        "mistral.mixtral"
    ):
        return MistralModel()
    elif model_id.startswith("cohere.command-r"):
        return CohereCommandModel()
    return CustomImportModel()


def get_embeddings_model(model_id: str) -> BedrockEmbeddingsModel:
    model_name = SUPPORTED_BEDROCK_EMBEDDING_MODELS.get(model_id, "")
    if DEBUG:
        logger.info("model name is " + model_name)
    match model_name:
        case "Cohere Embed Multilingual" | "Cohere Embed English":
            return CohereEmbeddingsModel()
        case _:
            logger.error("Unsupported model id " + model_id)
            raise HTTPException(
                status_code=400,
                detail="Unsupported embedding model id " + model_id,
            )
