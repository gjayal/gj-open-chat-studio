import datetime
import inspect
import json
import logging
import random
import time
import unicodedata
from typing import Annotated, Literal, Self

import tiktoken
from django.conf import settings
from django.core import validators
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import TextChoices
from jinja2.sandbox import SandboxedEnvironment
from langchain_core.messages import BaseMessage
from langchain_core.prompts import MessagesPlaceholder, PromptTemplate
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from langchain_core.tools import BaseTool
from langchain_openai.chat_models.base import OpenAIRefusalError
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langgraph.constants import END
from langgraph.types import Command, interrupt
from pydantic import BaseModel, BeforeValidator, Field, create_model, field_serializer, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic.config import ConfigDict
from pydantic_core import PydanticCustomError
from pydantic_core.core_schema import FieldValidationInfo
from RestrictedPython import compile_restricted, safe_builtins, safe_globals

from apps.annotations.models import TagCategories
from apps.assistants.models import OpenAiAssistant
from apps.chat.agent.tools import get_node_tools
from apps.chat.conversation import compress_chat_history, compress_pipeline_chat_history
from apps.documents.models import Collection
from apps.experiments.models import BuiltInTools, ExperimentSession, ParticipantData
from apps.pipelines.exceptions import AbortPipeline, PipelineNodeBuildError, PipelineNodeRunError, WaitForNextInput
from apps.pipelines.models import PipelineChatHistory, PipelineChatHistoryModes, PipelineChatHistoryTypes
from apps.pipelines.nodes.base import (
    NodeSchema,
    OptionsSource,
    PipelineNode,
    PipelineRouterNode,
    PipelineState,
    UiSchema,
    Widgets,
    deprecated_node,
)
from apps.pipelines.nodes.tool_callbacks import ToolCallbacks
from apps.pipelines.tasks import send_email_from_pipeline
from apps.service_providers.exceptions import ServiceProviderConfigError
from apps.service_providers.llm_service.adapters import AssistantAdapter, ChatAdapter
from apps.service_providers.llm_service.history_managers import PipelineHistoryManager
from apps.service_providers.llm_service.prompt_context import PromptTemplateContext
from apps.service_providers.llm_service.runnables import (
    AgentAssistantChat,
    AgentLLMChat,
    AssistantChat,
    ChainOutput,
    SimpleLLMChat,
)
from apps.service_providers.models import LlmProviderModel
from apps.utils.langchain import dict_to_json_schema
from apps.utils.prompt import OcsPromptTemplate, PromptVars, validate_prompt_variables

OptionalInt = Annotated[int | None, BeforeValidator(lambda x: None if isinstance(x, str) and len(x) == 0 else x)]


class OutputMessageTagMixin(BaseModel):
    tag: str = Field(
        default="",
        title="Message Tag",
        description="The tag that the output message should be tagged with",
    )

    @field_validator("tag", mode="after")
    @classmethod
    def normalize_tag(cls, value: str) -> str:
        return unicodedata.normalize("NFC", value)

    def get_output_tags(self) -> list[tuple[str, None]]:
        tags: list[tuple[str, None]] = []
        if self.tag:
            tags.append((self.tag, None))
        return tags


class RenderTemplate(PipelineNode, OutputMessageTagMixin):
    """Renders a Jinja template"""

    model_config = ConfigDict(
        json_schema_extra=NodeSchema(
            label="Render a template",
            icon="fa-solid fa-robot",
            documentation_link=settings.DOCUMENTATION_LINKS["node_template"],
        )
    )
    template_string: str = Field(
        description="Use {{your_variable_name}} to refer to designate input",
        json_schema_extra=UiSchema(widget=Widgets.expandable_text),
    )

    def _process(self, input, state: PipelineState, **kwargs) -> PipelineState:
        env = SandboxedEnvironment()
        try:
            content = {
                "input": input,
                "temp_state": state.get("temp_state", {}),
            }

            if "experiment_session" in state and state["experiment_session"]:
                exp_session = state["experiment_session"]
                participant = getattr(exp_session, "participant", None)
                if participant:
                    content.update(
                        {
                            "participant_details": {
                                "identifier": getattr(participant, "identifier", None),
                                "platform": getattr(participant, "platform", None),
                            },
                            "participant_schedules": participant.get_schedules_for_experiment(
                                exp_session.experiment,
                                as_dict=True,
                                include_inactive=True,
                            )
                            or [],
                        }
                    )
                proxy = self.get_participant_data_proxy(state)
                content["participant_data"] = proxy.get() or {}

            template = env.from_string(self.template_string)
            output = template.render(content)
        except Exception as e:
            raise PipelineNodeRunError(f"Error rendering template: {e}") from e

        return PipelineState.from_node_output(node_name=self.name, node_id=self.node_id, output=output)


class LLMResponseMixin(BaseModel):
    llm_provider_id: int = Field(..., title="LLM Model", json_schema_extra=UiSchema(widget=Widgets.llm_provider_model))
    llm_provider_model_id: int = Field(..., json_schema_extra=UiSchema(widget=Widgets.none))
    llm_temperature: float = Field(
        default=0.7, ge=0.0, le=2.0, title="Temperature", json_schema_extra=UiSchema(widget=Widgets.range)
    )

    @model_validator(mode="after")
    def validate_llm_model_deprecation(self):
        try:
            model = self.get_llm_provider_model()
        except PipelineNodeBuildError as e:
            raise PydanticCustomError(
                "invalid_model",
                str(e),
                {"field": "llm_provider_id"},
            ) from None
        if model.deprecated:
            raise PydanticCustomError(
                "deprecated_model",
                f"LLM provider model '{model.name}' is deprecated.",
                {"field": "llm_provider_id"},
            )
        return self

    def get_llm_service(self):
        from apps.service_providers.models import LlmProvider

        try:
            provider = LlmProvider.objects.get(id=self.llm_provider_id)
            return provider.get_llm_service()
        except LlmProvider.DoesNotExist:
            raise PipelineNodeBuildError(f"LLM provider with id {self.llm_provider_id} does not exist") from None
        except ServiceProviderConfigError as e:
            raise PipelineNodeBuildError("There was an issue configuring the LLM service provider") from e

    def get_llm_provider_model(self):
        try:
            return LlmProviderModel.objects.get(id=self.llm_provider_model_id)
        except LlmProviderModel.DoesNotExist:
            raise PipelineNodeBuildError(
                f"LLM provider model with id {self.llm_provider_model_id} does not exist"
            ) from None

    def get_chat_model(self):
        return self.get_llm_service().get_chat_model(self.get_llm_provider_model().name, self.llm_temperature)


class HistoryMixin(LLMResponseMixin):
    history_type: PipelineChatHistoryTypes = Field(
        PipelineChatHistoryTypes.NONE,
        json_schema_extra=UiSchema(widget=Widgets.history, enum_labels=PipelineChatHistoryTypes.labels),
    )
    history_name: str | None = Field(
        None,
        json_schema_extra=UiSchema(
            widget=Widgets.none,
        ),
    )
    history_mode: PipelineChatHistoryModes = Field(
        default=PipelineChatHistoryModes.SUMMARIZE,
        json_schema_extra=UiSchema(widget=Widgets.history_mode, enum_labels=PipelineChatHistoryModes.labels),
    )
    user_max_token_limit: int | None = Field(
        None,
        json_schema_extra=UiSchema(
            widget=Widgets.none,
        ),
    )
    max_history_length: int = Field(
        10,
        json_schema_extra=UiSchema(
            widget=Widgets.none,
        ),
    )

    @field_validator("history_name")
    def validate_history_name(cls, value, info: FieldValidationInfo):
        if info.data.get("history_type") == PipelineChatHistoryTypes.NAMED and not value:
            raise PydanticCustomError("invalid_history_name", "A history name is required for named history")
        return value

    def _get_history_name(self, node_id):
        if self.history_type == PipelineChatHistoryTypes.NAMED:
            return self.history_name
        return node_id

    def _get_history(self, session: ExperimentSession, node_id: str, input_messages: list) -> list[BaseMessage]:
        if self.history_type == PipelineChatHistoryTypes.NONE:
            return []

        if self.history_type == PipelineChatHistoryTypes.GLOBAL:
            return compress_chat_history(
                chat=session.chat,
                llm=self.get_chat_model(),
                max_token_limit=(
                    self.user_max_token_limit
                    if self.user_max_token_limit is not None
                    else self.get_llm_provider_model().max_token_limit
                ),
                input_messages=input_messages,
                history_mode=self.history_mode,
            )

        try:
            history: PipelineChatHistory = session.pipeline_chat_history.get(
                type=self.history_type, name=self._get_history_name(node_id)
            )
        except PipelineChatHistory.DoesNotExist:
            return []
        return compress_pipeline_chat_history(
            pipeline_chat_history=history,
            llm=self.get_chat_model(),
            max_token_limit=(
                self.user_max_token_limit
                if self.user_max_token_limit is not None
                else self.get_llm_provider_model().max_token_limit
            ),
            input_messages=input_messages,
            keep_history_len=self.max_history_length,
            history_mode=self.history_mode,
        )

    def _save_history(self, session: ExperimentSession, node_id: str, human_message: str, ai_message: str):
        if self.history_type == PipelineChatHistoryTypes.NONE:
            return

        if self.history_type == PipelineChatHistoryTypes.GLOBAL:
            # Global History is saved outside of the node
            return

        history, _ = session.pipeline_chat_history.get_or_create(
            type=self.history_type, name=self._get_history_name(node_id)
        )
        message = history.messages.create(human_message=human_message, ai_message=ai_message, node_id=node_id)
        return message


@deprecated_node(message="Use the 'LLM' node instead.")
class LLMResponse(PipelineNode, LLMResponseMixin):
    """Calls an LLM with the given input"""

    model_config = ConfigDict(json_schema_extra=NodeSchema(label="LLM response"))

    def _process(self, input: str, state: PipelineState) -> PipelineState:
        llm = self.get_chat_model()
        output = llm.invoke(input, config=self._config)
        return PipelineState.from_node_output(node_name=self.name, node_id=self.node_id, output=output.content)


class ToolConfigModel(BaseModel):
    allowed_domains: list[str] = Field(
        default_factory=list,
        json_schema_extra=UiSchema(
            widget=Widgets.none,
        ),
    )
    blocked_domains: list[str] = Field(
        default_factory=list,
        json_schema_extra=UiSchema(
            widget=Widgets.none,
        ),
    )

    @field_validator("allowed_domains", "blocked_domains", mode="after")
    @classmethod
    def validate_domains(cls, value: list[str], info) -> list[str]:
        values = list(map(str.strip, filter(None, value)))
        for value in values:
            try:
                validators.validate_domain_name(value)
            except ValidationError:
                raise ValueError(f"Invalid domain name '{value}' in field '{info.field_name}'") from None
        return values

    @field_serializer("allowed_domains", "blocked_domains")
    def serialize_lists(self, values: list[str]) -> list[str] | None:
        # return None instead of empty list
        return values if values else None


class LLMResponseWithPrompt(LLMResponse, HistoryMixin, OutputMessageTagMixin):
    """Uses and LLM to respond to the input."""

    model_config = ConfigDict(
        json_schema_extra=NodeSchema(
            label="LLM",
            icon="fa-solid fa-wand-magic-sparkles",
            documentation_link=settings.DOCUMENTATION_LINKS["node_llm"],
        )
    )

    source_material_id: OptionalInt = Field(
        None, json_schema_extra=UiSchema(widget=Widgets.select, options_source=OptionsSource.source_material)
    )
    prompt: str = Field(
        default="You are a helpful assistant. Answer the user's query as best you can",
        json_schema_extra=UiSchema(
            widget=Widgets.text_editor, options_source=OptionsSource.text_editor_autocomplete_vars_llm_node
        ),
    )
    collection_id: OptionalInt = Field(
        None,
        title="Media",
        json_schema_extra=UiSchema(
            widget=Widgets.select, options_source=OptionsSource.collection, flag_required="flag_pipelines-v2"
        ),
    )
    collection_index_id: OptionalInt = Field(
        None,
        title="Collection Index",
        json_schema_extra=UiSchema(
            widget=Widgets.select, options_source=OptionsSource.collection_index, flag_required="flag_pipelines-v2"
        ),
    )
    max_results: OptionalInt = Field(
        default=20,
        ge=1,
        le=100,
        description="The maximum number of results to retrieve from the index",
        json_schema_extra=UiSchema(widget=Widgets.range),
    )
    generate_citations: bool = Field(
        default=True,
        description="Allow files from this collection to be referenced in LLM responses and downloaded by users",
        json_schema_extra=UiSchema(widget=Widgets.toggle),
    )

    tools: list[str] = Field(
        default_factory=list,
        description="The tools to enable for the bot",
        json_schema_extra=UiSchema(widget=Widgets.multiselect, options_source=OptionsSource.agent_tools),
    )
    custom_actions: list[str] = Field(
        default_factory=list,
        description="Custom actions to enable for the bot",
        json_schema_extra=UiSchema(widget=Widgets.multiselect, options_source=OptionsSource.custom_actions),
    )
    built_in_tools: list[BuiltInTools] = Field(
        default_factory=list,
        description="Built in tools provided by the LLM model",
        json_schema_extra=UiSchema(widget=Widgets.built_in_tools, options_source=OptionsSource.built_in_tools),
    )
    tool_config: dict[str, ToolConfigModel] | None = Field(
        default_factory=dict,
        description="Configuration for builtin tools",
        json_schema_extra=UiSchema(widget=Widgets.none),
    )
    history_type: PipelineChatHistoryTypes = Field(
        PipelineChatHistoryTypes.GLOBAL,
        json_schema_extra=UiSchema(widget=Widgets.history, enum_labels=PipelineChatHistoryTypes.labels),
    )

    @model_validator(mode="after")
    def check_prompt_variables(self) -> Self:
        context = {
            "prompt": self.prompt,
            "source_material": self.source_material_id,
            "tools": self.tools,
            "media": self.collection_id,
        }
        try:
            # FUTURE TODO: add temp_state and session_state to PromptVars
            known_vars = set(PromptVars.values) | PromptVars.pipeline_extra_known_vars()
            validate_prompt_variables(context=context, prompt_key="prompt", known_vars=known_vars)
            return self
        except ValidationError as e:
            raise PydanticCustomError(
                "invalid_prompt", e.error_dict["prompt"][0].message, {"field": "prompt"}
            ) from None

    @field_validator("tools", "built_in_tools", mode="before")
    def ensure_value(cls, value: str):
        return value or []

    @field_validator("custom_actions", mode="before")
    def validate_custom_actions(cls, value):
        if value is None:
            return []
        return value

    @field_validator("collection_index_id", mode="before")
    def validate_collection_index_id(cls, value, info: FieldValidationInfo):
        if not value:
            return value

        collection = Collection.objects.get(id=value)
        if collection.llm_provider_id != info.data.get("llm_provider_id"):
            raise PydanticCustomError(
                "invalid_collection_index",
                f"The collection index and node must use the same LLM provider ({collection.llm_provider.name})",
            )
        return value

    def _process(self, input, state: PipelineState) -> PipelineState:
        session: ExperimentSession | None = state.get("experiment_session")
        # Get runnable
        provider_model = self.get_llm_provider_model()
        chat_model = self.get_chat_model()
        history_manager = PipelineHistoryManager.for_llm_chat(
            session=session,
            node_id=self.node_id,
            history_type=self.history_type,
            history_name=self.history_name,
            max_token_limit=provider_model.max_token_limit,
            chat_model=chat_model,
        )
        tool_callbacks = ToolCallbacks()

        # Tools setup
        tools = self._get_configured_tools(session=session, tool_callbacks=tool_callbacks)
        attachments = self._get_attachments(state)

        # Chat setup
        chat_adapter = ChatAdapter.for_pipeline(
            session=session,
            node=self,
            llm_service=self.get_llm_service(),
            provider_model=provider_model,
            tools=tools,
            pipeline_state=state,
            disabled_tools=self.disabled_tools,
            expect_citations=self.generate_citations,
        )
        allowed_tools = chat_adapter.get_allowed_tools()
        # TODO: tracing
        # if len(tools) != len(allowed_tools):
        # self.logger.info(
        #     "Some tools have been disabled: %s", [tool.name for tool in tools if tool not in allowed_tools]
        # )

        if allowed_tools:
            chat = AgentLLMChat(adapter=chat_adapter, history_manager=history_manager)
        else:
            chat = SimpleLLMChat(adapter=chat_adapter, history_manager=history_manager)
        # Invoke runnable
        result = chat.invoke(input=input, attachments=attachments)
        return PipelineState.from_node_output(
            node_name=self.name,
            node_id=self.node_id,
            output=result.output,
            output_message_metadata={
                **history_manager.output_message_metadata,
                **tool_callbacks.output_message_metadata,
            },
            intents=tool_callbacks.intents,
        )

    def _get_attachments(self, state: PipelineState) -> list:
        return [att for att in state.get("temp_state", {}).get("attachments", [])]

    def _get_configured_tools(
        self, session: ExperimentSession | None, tool_callbacks: ToolCallbacks
    ) -> list[dict | BaseTool]:
        """Get instantiated tools for the given node configuration."""
        tools = get_node_tools(self.django_node, session, tool_callbacks=tool_callbacks)
        tools.extend(self.get_llm_service().attach_built_in_tools(self.built_in_tools, self.tool_config))
        if self.collection_index_id:
            collection = Collection.objects.get(id=self.collection_index_id)
            tools.append(
                collection.get_search_tool(max_results=self.max_results, generate_citations=self.generate_citations)
            )

        return tools


class SendEmail(PipelineNode, OutputMessageTagMixin):
    """Send the input to the node to the list of addresses provided"""

    model_config = ConfigDict(
        json_schema_extra=NodeSchema(
            label="Send an email",
            icon="fa-solid fa-envelope",
            documentation_link=settings.DOCUMENTATION_LINKS["node_email"],
        )
    )

    recipient_list: str = Field(description="A comma-separated list of email addresses")
    subject: str

    @field_validator("recipient_list", mode="before")
    def recipient_list_has_valid_emails(cls, value):
        value = value or ""
        for email in [email.strip() for email in value.split(",")]:
            try:
                validate_email(email)
            except ValidationError:
                raise PydanticCustomError("invalid_recipient_list", "Invalid list of emails addresses") from None
        return value

    def _process(self, input, **kwargs) -> PipelineState:
        send_email_from_pipeline.delay(
            recipient_list=self.recipient_list.split(","), subject=self.subject, message=input
        )
        return PipelineState.from_node_output(node_name=self.name, node_id=self.node_id, output=input)


class Passthrough(PipelineNode):
    """Returns the input without modification"""

    model_config = ConfigDict(json_schema_extra=NodeSchema(label="Do Nothing", can_add=False))

    def _process(self, input, state: PipelineState) -> PipelineState:
        return PipelineState.from_node_output(node_name=self.name, node_id=self.node_id, output=input)


class StartNode(Passthrough):
    """The start of the pipeline"""

    name: str = "start"
    model_config = ConfigDict(json_schema_extra=NodeSchema(label="Start", flow_node_type="startNode"))


class EndNode(Passthrough):
    """The end of the pipeline"""

    name: str = "end"
    model_config = ConfigDict(json_schema_extra=NodeSchema(label="End", flow_node_type="endNode"))


@deprecated_node(message="Use the 'Router' node instead.")
class BooleanNode(PipelineRouterNode):
    """Branches based whether the input matches a certain value"""

    model_config = ConfigDict(json_schema_extra=NodeSchema(label="Conditional Node"))

    input_equals: str
    tag_output_message: bool = Field(
        default=False,
        description="Tag the output message with the selected route",
        json_schema_extra=UiSchema(widget=Widgets.toggle),
    )

    def _process_conditional(self, state: PipelineState) -> tuple[Literal["true", "false"], bool]:
        if self.input_equals == state["node_input"]:
            return "true", False
        return "false", False

    def get_output_map(self):
        """A mapping from the output handles on the frontend to the return values of _process_conditional"""
        return {"output_0": "true", "output_1": "false"}

    def get_output_tags(self, selected_route, is_default_keyword: bool) -> list[tuple[str, str]]:
        if self.tag_output_message:
            tag_name = f"{self.name}:{selected_route}"
            tag_category = TagCategories.ERROR if is_default_keyword else TagCategories.BOT_RESPONSE
            if is_default_keyword:
                tag_name += ":default"
            return [(tag_name, tag_category)]
        return []


class RouterMixin(BaseModel):
    keywords: list[str] = Field(default_factory=list, json_schema_extra=UiSchema(widget=Widgets.keywords))
    default_keyword_index: int = Field(default=0, json_schema_extra=UiSchema(widget=Widgets.none))
    tag_output_message: bool = Field(
        default=False,
        description="Tag the output message with the selected route",
        json_schema_extra=UiSchema(widget=Widgets.toggle),
    )

    @field_validator("keywords")
    def ensure_keywords_exist(cls, value, info: FieldValidationInfo):
        if not all(entry for entry in value):
            raise PydanticCustomError("invalid_keywords", "Keywords cannot be empty")
        if len(set(value)) != len(value):
            raise PydanticCustomError("invalid_keywords", "Keywords must be unique")
        return value

    def _create_router_schema(self):
        """Create a Pydantic model for structured router output"""
        return create_model(
            "RouterOutput", route=(Literal[tuple(self.keywords)], Field(description="Selected routing destination"))
        )

    def get_output_map(self):
        """Returns a mapping of the form:
        {"output_1": "keyword 1", "output_2": "keyword_2", ...} where keywords are defined by the user
        """
        return {f"output_{output_num}": keyword for output_num, keyword in enumerate(self.keywords)}

    def get_output_tags(self, selected_route, is_default_keyword: bool) -> list[tuple[str, str]]:
        if self.tag_output_message:
            tag_name = f"{self.name}:{selected_route}"
            tag_category = TagCategories.ERROR if is_default_keyword else TagCategories.BOT_RESPONSE
            if is_default_keyword:
                tag_name += ":default"
            return [(tag_name, tag_category)]
        return []


class RouterNode(RouterMixin, PipelineRouterNode, HistoryMixin):
    """Routes the input to one of the linked nodes using an LLM"""

    model_config = ConfigDict(
        json_schema_extra=NodeSchema(
            label="LLM Router",
            icon="fa-solid fa-arrows-split-up-and-left",
            documentation_link=settings.DOCUMENTATION_LINKS["node_llm_router"],
            field_order=[
                "llm_provider_id",
                "llm_temperature",
                "history_type",
                "prompt",
                "keywords",
                "tag_output_message",
            ],
        )
    )
    prompt: str = Field(
        default="You are an extremely helpful router",
        min_length=1,
        json_schema_extra=UiSchema(
            widget=Widgets.text_editor, options_source=OptionsSource.text_editor_autocomplete_vars_router_node
        ),
    )
    history_type: PipelineChatHistoryTypes = Field(
        PipelineChatHistoryTypes.NODE,
        json_schema_extra=UiSchema(widget=Widgets.history, enum_labels=PipelineChatHistoryTypes.labels),
    )

    @model_validator(mode="after")
    def check_prompt_variables(self) -> Self:
        context = {
            "prompt": self.prompt,
        }
        try:
            known_vars = {PromptVars.PARTICIPANT_DATA.value} | PromptVars.pipeline_extra_known_vars()
            validate_prompt_variables(context=context, prompt_key="prompt", known_vars=known_vars)
            return self
        except ValidationError as e:
            raise PydanticCustomError(
                "invalid_prompt", e.error_dict["prompt"][0].message, {"field": "prompt"}
            ) from None

    def _process_conditional(self, state: PipelineState):
        default_keyword = self.keywords[self.default_keyword_index] if self.keywords else None
        prompt = OcsPromptTemplate.from_messages(
            [
                ("system", f"{self.prompt}\nThe default routing destination is: {default_keyword}"),
                MessagesPlaceholder("history", optional=True),
                ("human", "{input}"),
            ]
        )
        session: ExperimentSession = state["experiment_session"]
        node_input = state["node_input"]
        context = {"input": node_input}
        extra_prompt_context = {
            "temp_state": state.get("temp_state", {}),
            "session_state": session.state or {},
        }
        context.update(PromptTemplateContext(session, extra=extra_prompt_context).get_context(prompt.input_variables))

        if self.history_type != PipelineChatHistoryTypes.NONE and session:
            input_messages = prompt.format_messages(**context)
            context["history"] = self._get_history(session, self.node_id, input_messages)

        llm = self.get_chat_model()
        router_schema = self._create_router_schema()
        chain = prompt | llm.with_structured_output(router_schema)
        is_default_keyword = False
        try:
            result = chain.invoke(context, config=self._config)
            keyword = getattr(result, "route", None)
        except PydanticValidationError:
            keyword = None
        except OpenAIRefusalError:
            keyword = default_keyword
            is_default_keyword = True
        if not keyword:
            keyword = default_keyword
            is_default_keyword = True

        if session:
            self._save_history(session, self.node_id, node_input, keyword)
        return keyword, is_default_keyword


class StaticRouterNode(RouterMixin, PipelineRouterNode):
    """Routes the input to a linked node using the temp state of the pipeline or participant data"""

    class DataSource(TextChoices):
        participant_data = "participant_data", "Participant Data"
        temp_state = "temp_state", "Temporary State"
        session_state = "session_state", "Session State"

    model_config = ConfigDict(
        json_schema_extra=NodeSchema(
            label="Static Router",
            icon="fa-solid fa-arrows-split-up-and-left",
            documentation_link=settings.DOCUMENTATION_LINKS["node_static_router"],
            field_order=["data_source", "route_key", "keywords"],
        )
    )

    data_source: DataSource = Field(
        DataSource.participant_data,
        description="The source of the data to use for routing",
        json_schema_extra=UiSchema(enum_labels=DataSource.labels),
    )
    route_key: str = Field(..., description="The key in the data to use for routing")

    def _process_conditional(self, state: PipelineState):
        from apps.service_providers.llm_service.prompt_context import SafeAccessWrapper

        match self.data_source:
            case self.DataSource.participant_data:
                data = self.get_participant_data_proxy(state).get()
            case self.DataSource.temp_state:
                data = state["temp_state"]
            case self.DataSource.session_state:
                data = state["experiment_session"].state

        formatted_key = f"{{data.{self.route_key}}}"
        try:
            result = formatted_key.format(data=SafeAccessWrapper(data))
        except KeyError:
            result = ""

        result_lower = result.lower()
        for keyword in self.keywords:
            if keyword.lower() == result_lower:
                return keyword, False
        return self.keywords[self.default_keyword_index], True


class ExtractStructuredDataNodeMixin:
    def _prompt_chain(self, reference_data):
        template = (
            "Extract user data using the current user data and conversation history as reference. Use JSON output."
            "\nCurrent user data:"
            "\n{reference_data}"
            "\nConversation history:"
            "\n{input}"
            "The conversation history should carry more weight in the outcome. It can change the user's current data"
        )
        prompt = PromptTemplate.from_template(template=template)
        return (
            {"input": RunnablePassthrough()}
            | RunnablePassthrough.assign(reference_data=RunnableLambda(lambda x: reference_data))
            | prompt
        )

    def extraction_chain(self, tool_class, reference_data):
        return self._prompt_chain(reference_data) | super().get_chat_model().with_structured_output(tool_class)

    def _process(self, input, state: PipelineState, **kwargs) -> PipelineState:
        ToolClass = self.get_tool_class(json.loads(self.data_schema))
        reference_data = self.get_reference_data(state)
        prompt_token_count = self._get_prompt_token_count(reference_data, ToolClass.model_json_schema())
        message_chunks = self.chunk_messages(input, prompt_token_count=prompt_token_count)

        new_reference_data = reference_data
        for message_chunk in message_chunks:
            chain = self.extraction_chain(tool_class=ToolClass, reference_data=new_reference_data)
            output = chain.invoke(message_chunk, config=self._config)
            output = output.model_dump()
            # TOOO: tracing
            # self.logger.info(
            #     f"Chunk {idx}",
            #     input=f"\nReference data:\n{new_reference_data}\nChunk data:\n{message_chunk}\n\n",
            #     output=f"\nExtracted data:\n{output}",
            # )
            new_reference_data = self.update_reference_data(output, reference_data)

        self.post_extraction_hook(new_reference_data, state)
        output = input if self.is_passthrough else json.dumps(new_reference_data)
        return PipelineState.from_node_output(node_name=self.name, node_id=self.node_id, output=output)

    def post_extraction_hook(self, output, state):
        pass

    def get_reference_data(self, state):
        return ""

    def update_reference_data(self, new_data: dict, reference_data: dict) -> dict:
        return new_data

    def _get_prompt_token_count(self, reference_data: dict | str, json_schema: dict) -> int:
        llm = super().get_chat_model()
        prompt_chain = self._prompt_chain(reference_data)
        # If we invoke the chain with an empty input, we get the prompt without the conversation history, which
        # is what we want.
        output = prompt_chain.invoke(input="")
        json_schema_tokens = llm.get_num_tokens(json.dumps(json_schema))
        return llm.get_num_tokens(output.text) + json_schema_tokens

    def chunk_messages(self, input: str, prompt_token_count: int) -> list[str]:
        """Chunk messages using a splitter that considers the token count.
        Strategy:
        - chunk_size (in tokens) = The LLM's token limit - prompt_token_count
        - chunk_overlap is chosen to be 20%

        Note:
        Since we don't know the token limit of the LLM, we assume it to be 8192.
        """
        llm_provider_model = self.get_llm_provider_model()
        model_token_limit = llm_provider_model.max_token_limit
        overlap_percentage = 0.2
        chunk_size_tokens = model_token_limit - prompt_token_count
        overlap_tokens = int(chunk_size_tokens * overlap_percentage)
        # TODO: tracing
        # self.logger.debug(f"Chunksize in tokens: {chunk_size_tokens} with {overlap_tokens} tokens overlap")

        try:
            encoding = tiktoken.encoding_for_model(llm_provider_model.name)
            encoding_name = encoding.name
        except KeyError:
            # The same encoder we use for llm.get_num_tokens_from_messages
            encoding_name = "gpt2"

        text_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name=encoding_name,
            chunk_size=chunk_size_tokens,
            chunk_overlap=overlap_tokens,
        )

        return text_splitter.split_text(input)

    def get_tool_class(self, data: dict):
        return dict_to_json_schema(data)


class StructuredDataSchemaValidatorMixin:
    @field_validator("data_schema")
    def validate_data_schema(cls, value):
        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError as e:
            raise PydanticCustomError("invalid_schema", "Invalid schema") from e

        if not isinstance(parsed_value, dict) or len(parsed_value) == 0:
            raise PydanticCustomError("invalid_schema", "Invalid schema")

        return value


class ExtractStructuredData(
    ExtractStructuredDataNodeMixin, LLMResponse, StructuredDataSchemaValidatorMixin, OutputMessageTagMixin
):
    """Extract structured data from the input"""

    model_config = ConfigDict(
        json_schema_extra=NodeSchema(
            label="Extract Structured Data",
            icon="fa-solid fa-database",
            documentation_link=settings.DOCUMENTATION_LINKS["node_extract_structured_data"],
        )
    )

    data_schema: str = Field(
        default='{"name": "the name of the user"}',
        description="A JSON object structure where the key is the name of the field and the value the description",
        json_schema_extra=UiSchema(widget=Widgets.expandable_text),
    )

    @property
    def is_passthrough(self) -> bool:
        return False


class ExtractParticipantData(
    ExtractStructuredDataNodeMixin, LLMResponse, StructuredDataSchemaValidatorMixin, OutputMessageTagMixin
):
    """Extract structured data and saves it as participant data"""

    model_config = ConfigDict(
        json_schema_extra=NodeSchema(
            label="Update Participant Data",
            icon="fa-solid fa-user-pen",
            documentation_link=settings.DOCUMENTATION_LINKS["node_update_participant_data"],
        )
    )

    data_schema: str = Field(
        default='{"name": "the name of the user"}',
        description="A JSON object structure where the key is the name of the field and the value the description",
        json_schema_extra=UiSchema(widget=Widgets.expandable_text),
    )
    key_name: str = ""

    @property
    def is_passthrough(self) -> bool:
        return True

    def get_reference_data(self, state) -> dict:
        """Returns the participant data as reference. If there is a `key_name`, the value in the participant data
        corresponding to that key will be returned insteadg
        """
        session = state.get("experiment_session")
        if not session:
            return {}

        participant_data = (
            ParticipantData.objects.for_experiment(session.experiment).filter(participant=session.participant).first()
        )
        if not participant_data:
            return {}

        data = participant_data.data
        if self.key_name:
            # string, list or dict
            return data.get(self.key_name, "")
        return data

    def update_reference_data(self, new_data: dict, reference_data: dict | list | str) -> dict:
        if isinstance(reference_data, dict):
            # new_data may be a subset, superset or wholly different set of keys than the reference_data, so merge
            return reference_data | new_data

        # if reference data is a string or list, we cannot merge, so let's override
        return new_data

    def post_extraction_hook(self, output, state):
        session = state.get("experiment_session")
        if not session:
            return

        if self.key_name:
            output = {self.key_name: output}

        try:
            participant_data = ParticipantData.objects.for_experiment(session.experiment).get(
                participant=session.participant
            )
            participant_data.data = participant_data.data | output
            participant_data.save()
        except ParticipantData.DoesNotExist:
            ParticipantData.objects.create(
                participant=session.participant,
                experiment=session.experiment,
                team=session.team,
                data=output,
            )


class AssistantNode(PipelineNode, OutputMessageTagMixin):
    """Calls an OpenAI assistant"""

    model_config = ConfigDict(
        json_schema_extra=NodeSchema(
            label="OpenAI Assistant",
            icon="fa-solid fa-user-tie",
            documentation_link=settings.DOCUMENTATION_LINKS["node_assistant"],
        )
    )

    assistant_id: int = Field(
        ..., json_schema_extra=UiSchema(widget=Widgets.select, options_source=OptionsSource.assistant)
    )
    citations_enabled: bool = Field(
        default=True,
        description="Whether to include cited sources in responses",
        json_schema_extra=UiSchema(widget=Widgets.toggle),
    )
    input_formatter: str = Field("", description="(Optional) Use {input} to designate the user input")

    @field_validator("input_formatter")
    def ensure_input_variable_exists(cls, value):
        value = value or ""
        acceptable_var = "input"
        if value:
            prompt_variables = set(PromptTemplate.from_template(value).input_variables)
            if acceptable_var not in prompt_variables:
                raise PydanticCustomError("invalid_input_formatter", "The input formatter must contain {input}")

            acceptable_vars = set([acceptable_var])
            extra_vars = prompt_variables - acceptable_vars
            if extra_vars:
                raise PydanticCustomError("invalid_input_formatter", "Only {input} is allowed")

    def _process(self, input, state: PipelineState, **kwargs) -> PipelineState:
        try:
            assistant = OpenAiAssistant.objects.get(id=self.assistant_id)
        except OpenAiAssistant.DoesNotExist:
            raise PipelineNodeBuildError(f"Assistant {self.assistant_id} does not exist") from None

        session: ExperimentSession | None = state.get("experiment_session")
        runnable = self._get_assistant_runnable(assistant, session=session)
        attachments = self._get_attachments(state)
        chain_output: ChainOutput = runnable.invoke(input, config=self._config, attachments=attachments)
        output = chain_output.output

        return PipelineState.from_node_output(
            node_name=self.name,
            node_id=self.node_id,
            output=output,
            input_message_metadata=runnable.history_manager.input_message_metadata or {},
            output_message_metadata=runnable.history_manager.output_message_metadata or {},
        )

    def _get_attachments(self, state) -> list:
        return [att for att in state.get("temp_state", {}).get("attachments", []) if att.upload_to_assistant]

    def _get_assistant_runnable(self, assistant: OpenAiAssistant, session: ExperimentSession):
        history_manager = PipelineHistoryManager.for_assistant()
        adapter = AssistantAdapter.for_pipeline(session=session, node=self, disabled_tools=self.disabled_tools)

        allowed_tools = adapter.get_allowed_tools()
        # TODO: tracing
        # if len(adapter.tools) != len(allowed_tools):
        #     self.logger.info(
        #         "Some tools have been disabled: %s",
        #         [tool.name for tool in adapter.tools if tool not in allowed_tools]
        #     )

        if allowed_tools:
            return AgentAssistantChat(adapter=adapter, history_manager=history_manager)
        else:
            if assistant.tools_enabled:
                logging.info("Tools have been disabled")
            return AssistantChat(adapter=adapter, history_manager=history_manager)


CODE_NODE_DOCS = f"{settings.DOCUMENTATION_BASE_URL}{settings.DOCUMENTATION_LINKS['node_code']}"
DEFAULT_FUNCTION = f"""# You must define a main function, which takes the node input as a string.
# Return a string to pass to the next node.

# Learn more about Python nodes at {CODE_NODE_DOCS}

def main(input: str, **kwargs) -> str:
    return input
"""


class CodeNode(PipelineNode, OutputMessageTagMixin):
    """Runs python"""

    model_config = ConfigDict(
        json_schema_extra=NodeSchema(
            label="Python Node",
            icon="fa-solid fa-file-code",
            documentation_link=settings.DOCUMENTATION_LINKS["node_code"],
        )
    )
    code: str = Field(
        default=DEFAULT_FUNCTION,
        description="The code to run",
        json_schema_extra=UiSchema(widget=Widgets.code),
    )

    @field_validator("code")
    def validate_code(cls, value, info: FieldValidationInfo):
        if not value:
            value = DEFAULT_FUNCTION
        try:
            byte_code = compile_restricted(
                value,
                filename="<inline code>",
                mode="exec",
            )
            custom_locals = {}
            try:
                exec(byte_code, {}, custom_locals)
            except Exception as exc:
                raise PydanticCustomError("invalid_code", "{error}", {"error": str(exc)}) from exc

            try:
                main = custom_locals["main"]
            except KeyError:
                raise SyntaxError("You must define a 'main' function") from None

            for name, item in custom_locals.items():
                if name != "main" and inspect.isfunction(item):
                    raise SyntaxError(
                        "You can only define a single function, 'main' at the top level. "
                        "You may use nested functions inside that function if required"
                    )

            if list(inspect.signature(main).parameters) != ["input", "kwargs"]:
                raise SyntaxError("The main function should have the signature main(input, **kwargs) only.")

        except SyntaxError as exc:
            raise PydanticCustomError("invalid_code", "{error}", {"error": exc.msg}) from None
        return value

    def _process(self, input: str, state: PipelineState) -> PipelineState | Command:
        function_name = "main"
        byte_code = compile_restricted(
            self.code,
            filename="<inline code>",
            mode="exec",
        )

        custom_locals = {}
        output_state = PipelineState()
        custom_globals = self._get_custom_globals(self.node_id, state, output_state)
        kwargs = {}
        try:
            exec(byte_code, custom_globals, custom_locals)
            result = custom_locals[function_name](input, **kwargs)
        except WaitForNextInput:
            return Command(goto=END)
        except AbortPipeline as abort:
            return interrupt(abort.to_json())
        except Exception as exc:
            raise PipelineNodeRunError(exc) from exc

        if isinstance(result, Command):
            return result
        return Command(
            goto=self._outgoing_nodes,
            update=PipelineState.from_node_output(
                node_name=self.name, node_id=self.node_id, output=str(result), **output_state
            ),
        )

    def _get_custom_globals(self, node_id, state: PipelineState, output_state: PipelineState) -> dict:
        """
        Args:
            node_id: This node's ID
            state: The input state. Do not modify this state.
            output_state: An empty state dict to which state modifications should be made.
        """
        from RestrictedPython.Eval import (
            default_guarded_getitem,
            default_guarded_getiter,
        )

        custom_globals = safe_globals.copy()

        participant_data_proxy = self.get_participant_data_proxy(state)
        pipeline_state = PipelineState.clone(state)

        # copy this from input to output to create a consistent view within the code execution
        output_state["temp_state"] = pipeline_state.get("temp_state") or {}

        # add this node into the state so that we can trace the path
        pipeline_state["outputs"] = {**state["outputs"], self.name: {"node_id": node_id}}
        custom_globals.update(
            {
                "__builtins__": self._get_custom_builtins(),
                "json": json,
                "datetime": datetime,
                "time": time,
                "_getitem_": default_guarded_getitem,
                "_getiter_": default_guarded_getiter,
                "_write_": lambda x: x,
                "get_participant_data": participant_data_proxy.get,
                "set_participant_data": participant_data_proxy.set,
                "get_participant_schedules": participant_data_proxy.get_schedules,
                "get_temp_state_key": self._get_temp_state_key(output_state),
                "set_temp_state_key": self._set_temp_state_key(output_state),
                "get_session_state_key": self._get_session_state_key(state["experiment_session"]),
                "set_session_state_key": self._set_session_state_key(state["experiment_session"]),
                "get_selected_route": pipeline_state.get_selected_route,
                "get_node_path": pipeline_state.get_node_path,
                "get_all_routes": pipeline_state.get_all_routes,
                "add_message_tag": output_state.add_message_tag,
                "add_session_tag": output_state.add_session_tag,
                "get_node_output": pipeline_state.get_node_output_by_name,
                # control flow
                "abort_with_message": self._abort_pipeline(),
                "require_node_outputs": self._require_node_outputs(state),
            }
        )
        return custom_globals

    def _abort_pipeline(self):
        def abort_pipeline(message, tag_name: str = None):
            raise AbortPipeline(message, tag_name)

        return abort_pipeline

    def _require_node_outputs(self, state: PipelineState):
        """A helper function to require inputs from a specific node"""

        def require_node_outputs(*node_names):
            if len(node_names) == 1 and isinstance(node_names[0], list):
                node_names = node_names[0]
            if not all(isinstance(name, str) for name in node_names):
                raise PipelineNodeRunError("Node names passed to 'require_node_outputs' must be a string")
            for node_name in node_names:
                if node_name not in state["outputs"]:
                    raise WaitForNextInput(f"Node '{node_name}' has not produced any output yet")

        return require_node_outputs

    def _get_session_state_key(self, session: ExperimentSession):
        def get_session_state_key(key_name: str):
            return session.state.get(key_name)

        return get_session_state_key

    def _set_session_state_key(self, session: ExperimentSession):
        def set_session_state_key(key_name: str, value):
            session.state[key_name] = value
            session.save(update_fields=["state"])

        return set_session_state_key

    def _get_temp_state_key(self, state: PipelineState):
        def get_temp_state_key(key_name: str):
            return state["temp_state"].get(key_name)

        return get_temp_state_key

    def _set_temp_state_key(self, state: PipelineState):
        def set_temp_state_key(key_name: str, value):
            if key_name in {"user_input", "outputs", "attachments"}:
                raise PipelineNodeRunError(f"Cannot set the '{key_name}' key of the temporary state")
            state["temp_state"][key_name] = value

        return set_temp_state_key

    def _get_custom_builtins(self):
        allowed_modules = {
            "json",
            "re",
            "datetime",
            "time",
            "random",
        }
        custom_builtins = safe_builtins.copy()
        custom_builtins.update(
            {
                "min": min,
                "max": max,
                "sum": sum,
                "abs": abs,
                "all": all,
                "any": any,
                "datetime": datetime,
                "random": random,
            }
        )

        def guarded_import(name, *args, **kwargs):
            if name not in allowed_modules:
                raise ImportError(f"Importing '{name}' is not allowed")
            return __import__(name, *args, **kwargs)

        custom_builtins["__import__"] = guarded_import
        return custom_builtins
