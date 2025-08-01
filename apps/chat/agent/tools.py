import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar, Union

from django.db import transaction, utils
from langchain_community.utilities.openapi import OpenAPISpec
from langchain_core.tools import BaseTool
from pgvector.django import CosineDistance

from apps.channels.models import ChannelPlatform
from apps.chat.agent import schemas
from apps.chat.agent.openapi_tool import openapi_spec_op_to_function_def
from apps.chat.models import ChatAttachment
from apps.events.forms import ScheduledMessageConfigForm
from apps.events.models import ScheduledMessage, TimePeriod
from apps.experiments.models import AgentTools, Experiment, ExperimentSession, ParticipantData
from apps.files.models import FileChunkEmbedding
from apps.pipelines.models import Node
from apps.pipelines.nodes.tool_callbacks import ToolCallbacks
from apps.utils.time import pretty_date

if TYPE_CHECKING:
    from apps.assistants.models import OpenAiAssistant
    from apps.pipelines.models import Node


OCS_CITATION_PATTERN = r"<CIT\s+(?P<file_id>\d+)\s*/>"

SUCCESSFUL_ATTACHMENT_MESSAGE: str = "File {file_id} ({name}) is attached to your response"

CREATE_LINK_TEXT = """You can use this markdown link to reference it in your response:
    `[{name}](file:{team_slug}:{session_id}:{file_id})` or `![](file:{team_slug}:{session_id}:{file_id})`
    if it is an image.
"""

CHUNK_TEMPLATE = """
## File name: {file_name}, file_id={file_id}
### Content
{chunk}
"""

CITATION_PROMPT = """**CRITICAL REQUIREMENT - MANDATORY CITATIONS:**

You MUST cite all information using this exact format: <CIT file-id />

**Citation Rules:**
- Place citations immediately after each sentence or claim that references retrieved content
- Use the specific file ID from the source document
- Example: "The revenue increased by 15% last quarter <CIT 123 />.". In this example, "123" is the file ID of the
source document.
- NEVER provide information from retrieved files without proper citations

**Response Structure:**
1. Answer the user's question thoroughly
2. Support each claim with evidence from the files
3. Ensure every factual statement includes a citation
4. If no relevant information exists in the files, explicitly state this

Failure to include proper citations will result in an incomplete response.
"""


@dataclass
class SearchToolConfig:
    index_id: int
    max_results: int = 5
    generate_citations: bool = True

    def get_index(self):
        from apps.documents.models import Collection

        return Collection.objects.get(id=self.index_id)


class CustomBaseTool(BaseTool):
    requires_callbacks: ClassVar[bool] = False

    experiment_session: ExperimentSession | None = None
    # Some tools like the reminder requires a chat session id in order to get back to the user later
    requires_session: bool = False
    tool_callbacks: ToolCallbacks | None = None

    def _run(self, *args, **kwargs):
        if self.requires_session and not self.experiment_session:
            return "I am unable to do this"
        try:
            return self.action(*args, **kwargs)
        except Exception as e:
            logging.exception(e)
            return "Something went wrong"

    async def _arun(self, *args, **kwargs) -> str:
        """Use the tool asynchronously."""
        raise NotImplementedError("custom_search does not support async")

    def action(self, *args, **kwargs):
        raise Exception("Not implemented")


class RecurringReminderTool(CustomBaseTool):
    name: str = AgentTools.RECURRING_REMINDER
    description: str = "Schedule recurring reminders"
    requires_session: bool = True
    args_schema: type[schemas.RecurringReminderSchema] = schemas.RecurringReminderSchema

    def action(
        self,
        schedule_name: str,
        datetime_due: datetime,
        every: int,
        period: TimePeriod,
        message: str,
        datetime_end: datetime | None = None,
        repetitions: int | None = None,
    ):
        return create_schedule_message(
            self.experiment_session,
            message=message,
            name=schedule_name,
            start_date=datetime_due,
            end_date=datetime_end,
            repetitions=repetitions,
            frequency=every,
            time_period=period,
            is_recurring=True,
        )


class OneOffReminderTool(CustomBaseTool):
    name: str = AgentTools.ONE_OFF_REMINDER
    description: str = "Schedule one-off reminders"
    requires_session: bool = True
    args_schema: type[schemas.OneOffReminderSchema] = schemas.OneOffReminderSchema

    def action(
        self,
        datetime_due: datetime,
        message: str,
        schedule_name: str,
    ):
        return create_schedule_message(
            self.experiment_session, message=message, name=schedule_name, start_date=datetime_due, is_recurring=False
        )


class MoveScheduledMessageDateTool(CustomBaseTool):
    name: str = AgentTools.MOVE_SCHEDULED_MESSAGE_DATE
    description: str = "Move the day and time that the scheduled message should trigger"
    requires_session: bool = True
    args_schema: type[schemas.ScheduledMessageSchema] = schemas.ScheduledMessageSchema

    def action(
        self,
        message_id: str,
        weekday: schemas.WeekdaysEnum,
        hour: int,
        minute: int,
        specified_date: datetime | None,
    ):
        try:
            message = ScheduledMessage.objects.get(
                participant=self.experiment_session.participant, external_id=message_id
            )
        except ScheduledMessage.DoesNotExist:
            return f"The scheduled message with id={message_id} was not found."
        if specified_date and message.was_created_by_system:
            # When the user specifies a new date, the bot will extract the day of the week that that day falls on
            # and pass it as a parameter to this method.
            # Since we only allow users to change the weekday of their schedules, this bahvaiour can lead to a
            # confusing conversation where the bot updated their schedule to a seemingly random date that
            # corresponds to the same weekday as the requested day. To resolve this, we simply don't allow users
            # to specify dates, but only a weekday and the time of day.
            return "The user cannot do that. Only weekdays and time of day can be changed"

        # the datetime object regard Monday as day 0 whereas the llm regards it as day 1
        if specified_date:
            message.next_trigger_date = specified_date
        else:
            weekday_int = weekday.value - 1
            message.next_trigger_date = _move_datetime_to_new_weekday_and_time(
                message.next_trigger_date, weekday_int, hour, minute
            )
        message.save()

        return f"The new datetime is {pretty_date(message.next_trigger_date)}"


class DeleteReminderTool(CustomBaseTool):
    name: str = AgentTools.DELETE_REMINDER
    description: str = "Delete scheduled reminders"
    requires_session: bool = True
    args_schema: type[schemas.DeleteReminderSchema] = schemas.DeleteReminderSchema

    def action(self, message_id: str):
        try:
            scheduled_message = self.experiment_session.participant.schduled_messages.get(external_id=message_id)
            if scheduled_message.was_created_by_system:
                # Participants should not be able to delete a scheduled message that was created through an action
                return "Cannot delete this reminder"
        except ScheduledMessage.DoesNotExist:
            return "Could not find this reminder"

        scheduled_message.cancel()
        return "Success"


class UpdateParticipantDataTool(CustomBaseTool):
    name: str = AgentTools.UPDATE_PARTICIPANT_DATA
    description: str = "Update user data"
    requires_session: bool = True
    args_schema: type[schemas.UpdateUserDataSchema] = schemas.UpdateUserDataSchema

    @transaction.atomic
    def action(self, key: str, value: Any):
        try:
            participant_data = ParticipantData.objects.for_experiment(self.experiment_session.experiment).get(
                participant=self.experiment_session.participant
            )
            participant_data.data[key] = value
            participant_data.save()
        except ParticipantData.DoesNotExist:
            ParticipantData.objects.create(
                participant=self.experiment_session.participant,
                experiment=self.experiment_session.experiment,
                team=self.experiment_session.team,
                data={key: value},
            )
        return "Success"


class EndSessionTool(CustomBaseTool):
    requires_callbacks: ClassVar[bool] = True
    name: str = AgentTools.END_SESSION
    description: str = (
        "End the current chat session. "
        "This will mark the session as completed. "
        "New messages will result in a new session being created."
    )

    def action(self, *args, **kwargs):
        from apps.pipelines.nodes.base import Intents

        self.tool_callbacks.register_intent(Intents.END_SESSION)
        return "Your intent to end the session has been registered."


class AttachMediaTool(CustomBaseTool):
    requires_callbacks: ClassVar[bool] = True
    name: str = AgentTools.ATTACH_MEDIA
    description: str = "Attach a media file to your response"
    requires_session: bool = True
    args_schema: type[schemas.AttachMediaSchema] = schemas.AttachMediaSchema

    @cached_property
    def chat_attachment(self) -> ChatAttachment:
        chat_attachment, _ = ChatAttachment.objects.get_or_create(
            chat=self.experiment_session.chat, tool_type="ocs_attachments"
        )
        return chat_attachment

    @transaction.atomic
    def action(self, file_id: int) -> str:
        from apps.files.models import File

        try:
            file = File.objects.get(id=file_id)
            self.chat_attachment.files.add(file_id)
            self.tool_callbacks.attach_file(file_id)
            response = SUCCESSFUL_ATTACHMENT_MESSAGE.format(file_id=file_id, name=file.name)

            if self.experiment_session.experiment_channel.platform == ChannelPlatform.WEB:
                # Only the web platform is able to render these links
                link_text = CREATE_LINK_TEXT.format(
                    name=file.name, file_id=file_id, session_id=self.experiment_session.id, team_slug=file.team.slug
                )
                response = f"{response}. {link_text}"
            else:
                response = f"{response}. Do not use markdown links to reference the file."
            return response
        except File.DoesNotExist:
            return f"File '{file_id}' does not exist"
        except utils.IntegrityError:
            return f"Unable to attach file '{file_id}' to the message"


class SearchIndexTool(CustomBaseTool):
    name: str = AgentTools.SEARCH_INDEX
    description: str = "Search files / source material for relevant information pertaining to the user's query"
    requires_session: bool = False
    args_schema: type[schemas.SearchIndexSchema] = schemas.SearchIndexSchema
    search_config: SearchToolConfig

    @transaction.atomic
    def action(self, query: str) -> str:
        """
        Do a simple search for the top most relevant file chunks based on the query provided by the user. A little query
        rewriting is automatically done by the LLM, since it decides what query to use when invoking this tool.
        """
        # - [ ] Generate references
        index = self.search_config.get_index()
        max_results = self.search_config.max_results

        query_vector = index.get_query_vector(query)
        # This query is automatically team scoped
        embeddings = (
            FileChunkEmbedding.objects.annotate(distance=CosineDistance("embedding", query_vector))
            .filter(collection_id=index.id)
            .order_by("distance")
            .select_related("file")
            .only("text", "file__name")[:max_results]
        )
        retrieved_chunks = "".join([self._format_result(embedding) for embedding in embeddings])
        response_template = """
# Retrieved chunks
{retrieved_chunks}
{citation_prompt}
"""
        citation_prompt = CITATION_PROMPT if self.search_config.generate_citations else ""
        return response_template.format(retrieved_chunks=retrieved_chunks, citation_prompt=citation_prompt)

    def _format_result(self, embedding: FileChunkEmbedding) -> str:
        """
        Format the result from the search index into a more structured format.
        """

        return CHUNK_TEMPLATE.format(file_name=embedding.file.name, file_id=embedding.file_id, chunk=embedding.text)


def _move_datetime_to_new_weekday_and_time(date: datetime, new_weekday: int, new_hour: int, new_minute: int):
    current_weekday = date.weekday()
    day_diff = new_weekday - current_weekday
    return date.replace(hour=new_hour, minute=new_minute, second=0) + timedelta(days=day_diff)


def create_schedule_message(
    experiment_session: ExperimentSession,
    message: str,
    name: str,
    start_date: datetime,
    is_recurring: bool,
    end_date: datetime | None = None,
    **kwargs,
):
    kwargs["name"] = name
    kwargs["prompt_text"] = message
    kwargs["experiment_id"] = experiment_session.experiment.id

    if is_recurring:
        non_required_fields = ["repetitions"]
    else:
        kwargs["repetitions"] = 0
        non_required_fields = ["frequency", "time_period"]

    form = ScheduledMessageConfigForm(
        data=kwargs, experiment_id=experiment_session.experiment.id, non_required_fields=non_required_fields
    )
    if form.is_valid():
        cleaned_data = form.cleaned_data
        try:
            with transaction.atomic():
                ScheduledMessage.objects.create(
                    custom_schedule_params={
                        "name": cleaned_data["name"],
                        "prompt_text": cleaned_data["prompt_text"],
                        "frequency": cleaned_data.get("frequency"),
                        "time_period": cleaned_data.get("time_period"),
                        "repetitions": cleaned_data.get("repetitions"),
                    },
                    experiment=experiment_session.experiment,
                    participant=experiment_session.participant,
                    team=experiment_session.team,
                    next_trigger_date=start_date,
                    end_date=end_date,
                )
            return "Success: scheduled message created"
        except Experiment.DoesNotExist:
            return "Experiment does not exist! Could not create scheduled message"
    logging.exception(f"Could not create one-off reminder. Form errors: {form.errors}")
    return "Could not create scheduled message"


TOOL_CLASS_MAP = {
    AgentTools.MOVE_SCHEDULED_MESSAGE_DATE: MoveScheduledMessageDateTool,
    AgentTools.ONE_OFF_REMINDER: OneOffReminderTool,
    AgentTools.RECURRING_REMINDER: RecurringReminderTool,
    AgentTools.DELETE_REMINDER: DeleteReminderTool,
    AgentTools.UPDATE_PARTICIPANT_DATA: UpdateParticipantDataTool,
    AgentTools.END_SESSION: EndSessionTool,
    AgentTools.ATTACH_MEDIA: AttachMediaTool,
    AgentTools.SEARCH_INDEX: SearchIndexTool,
}


def get_tools(experiment_session, experiment) -> list[BaseTool]:
    tool_holder = experiment.assistant if experiment.assistant else experiment
    tools = get_tool_instances(tool_holder.tools, experiment_session)
    tools.extend(get_custom_action_tools(tool_holder))
    return tools


def get_assistant_tools(assistant, experiment_session: ExperimentSession | None = None) -> list[BaseTool]:
    tools = get_tool_instances(assistant.tools, experiment_session)
    tools.extend(get_custom_action_tools(assistant))
    return tools


def get_node_tools(
    node: Node, experiment_session: ExperimentSession | None = None, tool_callbacks: ToolCallbacks | None = None
) -> list[BaseTool]:
    tool_names = node.params.get("tools") or []
    if node.requires_attachment_tool():
        tool_names.append(AgentTools.ATTACH_MEDIA)
    tools = get_tool_instances(tool_names, experiment_session, tool_callbacks)
    tools.extend(get_custom_action_tools(node))
    return tools


def get_tool_instances(
    tools_list, experiment_session: ExperimentSession | None = None, tool_callbacks=None
) -> list[BaseTool]:
    tools = []
    for tool_name in tools_list:
        tool_cls = TOOL_CLASS_MAP[tool_name]
        if tool_cls.requires_callbacks and not tool_callbacks:
            raise ValueError(f"Tool {tool_name} requires callbacks but none were provided")
        tools.append(tool_cls(experiment_session=experiment_session, tool_callbacks=tool_callbacks))
    return tools


def get_custom_action_tools(action_holder: Union[Experiment, "OpenAiAssistant", "Node"]) -> list[BaseTool]:
    operations = action_holder.get_custom_action_operations().select_related("custom_action__auth_provider").all()
    return list(filter(None, [get_tool_for_custom_action_operation(operation) for operation in operations]))


def get_tool_for_custom_action_operation(custom_action_operation) -> BaseTool | None:
    custom_action = custom_action_operation.custom_action
    spec = OpenAPISpec.from_spec_dict(custom_action_operation.operation_schema)
    if not spec.paths:
        return

    auth_service = custom_action.get_auth_service()
    path = list(spec.paths)[0]
    method = spec.get_methods_for_path(path)[0]
    function_def = openapi_spec_op_to_function_def(spec, path, method)
    return function_def.build_tool(auth_service)
