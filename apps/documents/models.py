from collections.abc import Iterator

import pydantic
from django.db import models, transaction
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django_pydantic_field import SchemaField
from field_audit import audit_fields
from field_audit.models import AuditingManager

from apps.chat.agent.tools import SearchIndexTool, SearchToolConfig
from apps.documents.exceptions import IndexConfigurationException
from apps.experiments.versioning import VersionDetails, VersionField, VersionsMixin, VersionsObjectManagerMixin
from apps.files.models import File
from apps.service_providers.llm_service.main import OpenAIBuiltinTool
from apps.service_providers.models import EmbeddingProviderModel
from apps.teams.models import BaseTeamModel
from apps.utils.conversions import bytes_to_megabytes
from apps.utils.deletion import (
    get_related_m2m_objects,
    get_related_pipeline_experiments_queryset,
    get_related_pipelines_queryset,
)


class ChunkingStrategy(pydantic.BaseModel):
    chunk_size: int = pydantic.Field(description="Size of each chunk in tokens")
    chunk_overlap: int = pydantic.Field(description="Number of overlapping tokens between chunks")


class CollectionFileMetadata(pydantic.BaseModel):
    chunking_strategy: ChunkingStrategy = pydantic.Field(description="Chunking strategy used for the file")


class CollectionObjectManager(VersionsObjectManagerMixin, AuditingManager):
    pass


class FileStatus(models.TextChoices):
    # See https://platform.openai.com/docs/api-reference/vector-stores-files/file-object
    PENDING = ("pending", _("Pending"))
    IN_PROGRESS = ("in_progress", _("In Progress"))
    COMPLETED = "completed", _("Completed")
    FAILED = "failed", _("Failed")


class CollectionFile(models.Model):
    file = models.ForeignKey("files.File", on_delete=models.CASCADE)
    collection = models.ForeignKey("documents.Collection", on_delete=models.CASCADE)
    status = models.CharField(max_length=64, choices=FileStatus.choices, blank=True)
    metadata = SchemaField(schema=CollectionFileMetadata, null=True)

    def __str__(self) -> str:
        return f"{self.file.name} in {self.collection.name}"

    @property
    def file_size_mb(self):
        return self.file.size_mb

    @property
    def chunking_strategy(self) -> ChunkingStrategy | None:
        if self.metadata:
            return self.metadata.chunking_strategy

    @property
    def status_enum(self):
        return FileStatus(self.status)


@audit_fields(
    "name",
    "version_number",
    "llm_provider",
    "is_index",
    "is_remote_index",
    "embedding_provider_model",
    "openai_vector_store_id",
    audit_special_queryset_writes=True,
)
class Collection(BaseTeamModel, VersionsMixin):
    name = models.CharField(max_length=255)
    files = models.ManyToManyField("files.File", blank=False, through=CollectionFile, related_name="collections")
    working_version = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="versions",
    )
    is_archived = models.BooleanField(default=False)
    version_number = models.PositiveIntegerField(default=1)
    llm_provider = models.ForeignKey(
        "service_providers.LlmProvider",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="LLM Provider",
    )
    embedding_provider_model = models.ForeignKey(
        EmbeddingProviderModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="collection_embedding_model",
    )
    is_remote_index = models.BooleanField(
        default=False, help_text="If selected, this index will be created at and hosted by the selected provider"
    )
    openai_vector_store_id = models.CharField(blank=True, max_length=255)
    is_index = models.BooleanField(default=False)

    objects = CollectionObjectManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "name", "version_number", "working_version_id"],
                name="unique_collection_version_per_team",
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def index_name(self) -> str:
        name = f"collection-{self.team.slug}-{slugify(self.name)}-{self.id}"
        if self.is_a_version:
            return f"{name} v{self.version_number}"
        return name

    @property
    def size(self) -> float:
        """Returns the size of this collection in megabytes"""
        bytes = self.files.aggregate(bytes=models.Sum("content_size"))["bytes"] or 0
        return bytes_to_megabytes(bytes)

    def _get_version_details(self) -> VersionDetails:
        return VersionDetails(
            instance=self,
            fields=[
                VersionField(group_name="General", name="name", raw_value=self.name),
                VersionField(group_name="General", name="llm_provider", raw_value=self.llm_provider),
                VersionField(
                    group_name="General", name="embedding_provider_model", raw_value=self.embedding_provider_model
                ),
                VersionField(group_name="General", name="files", queryset=self.files.all()),
            ],
        )

    @transaction.atomic()
    def create_new_version(self, save=True):
        """
        When a collection is indexed, we need to create a new vector store when we create a new version of it
        and upload the file versions to it.
        """
        from apps.documents.tasks import index_collection_files

        version_number = self.version_number
        self.version_number = version_number + 1
        self.save(update_fields=["version_number"])

        new_version = super().create_new_version(save=False)
        new_version.version_number = version_number
        new_version.openai_vector_store_id = ""
        new_version.save()

        file_versions: dict[int, int] = {}
        for file in self.files.iterator(chunk_size=15):
            file_version = file.create_new_version(save=False)
            file_version.external_id = ""
            file_version.external_source = ""
            file_version.save()
            file_versions[file.id] = file_version.id

        new_version.files.add(*list(file_versions.values()))

        if self.is_index:
            # Create a new vector store at llm service for the new version of the collection.
            # Optimization suggestion: Only when the file set changed, should we create a new vector store at the
            # provider
            if self.is_remote_index:
                new_version.ensure_remote_index_created()

                # Upload files to vector store
                if collection_files := CollectionFile.objects.filter(collection_id=new_version.id):
                    index_collection_files(collection_files)
            else:
                # Create versions of file chunk embeddings and add them to the new collection
                for embedding in self.filechunkembedding_set.iterator(chunk_size=50):
                    embedding_version = embedding.create_new_version(save=False)
                    embedding_version.collection = new_version

                    file_version_id = file_versions[embedding.file_id]
                    embedding_version.file_id = file_version_id
                    embedding_version.save()

        return new_version

    def get_absolute_url(self):
        return reverse("documents:single_collection_home", args=[self.team.slug, self.id])

    def get_related_nodes_queryset(self) -> models.QuerySet:
        index_references = get_related_pipelines_queryset(self, "collection_index_id").distinct()
        collection_references = get_related_pipelines_queryset(self, "collection_id").distinct()
        return index_references | collection_references

    def get_related_experiments_queryset(self) -> models.QuerySet:
        """
        Get all experiments that reference this collection through a pipeline. This includes both published and working
        experiments. When check_versions is True, it will return all experiments that reference any version of this
        collection.
        """
        # TODO: Update assistant archive code to use get_related_pipeline_experiments_queryset
        ids = list(self.versions.values_list("id", flat=True)) + [self.id]

        index_references = get_related_pipeline_experiments_queryset(ids, "collection_index_id").filter(
            models.Q(is_default_version=True) | models.Q(working_version__id__isnull=True),
        )
        collection_references = get_related_pipeline_experiments_queryset(ids, "collection_id").filter(
            models.Q(is_default_version=True) | models.Q(working_version__id__isnull=True),
        )
        return index_references | collection_references

    @transaction.atomic()
    def archive(self):
        """
        Archive the collection with its files and remove the index and the files at the remote service, if it has one
        """
        if self.get_related_nodes_queryset().exists():
            return False

        if self.is_working_version and self.get_related_experiments_queryset().exists():
            return False

        super().archive()

        files = list(self.files.all())
        # Remove the references to the files in the collection
        CollectionFile.objects.filter(collection=self).delete()

        # Cleanup conditionally
        files_with_references = get_related_m2m_objects(files)
        unused_files = [file for file in files if file not in files_with_references]
        unused_file_ids = [file.id for file in unused_files]

        if self.is_index and self.openai_vector_store_id:
            self._remove_remote_index(unused_files)

        File.objects.filter(id__in=unused_file_ids).update(is_archived=True)
        return True

    def has_failed_index_uploads(self) -> bool:
        """
        Check if any of the files in this collection failed to upload to an index
        """
        return CollectionFile.objects.filter(
            collection=self,
            status=FileStatus.FAILED,
        ).exists()

    def has_pending_index_uploads(self) -> bool:
        """
        Check if any of the files in this collection are not yet uploaded to an index
        """
        return CollectionFile.objects.filter(
            collection=self,
            status__in=[FileStatus.PENDING, FileStatus.IN_PROGRESS],
        ).exists()

    def _remove_remote_index(self, remote_files_to_remove: list[File]):
        """Remove the index backend"""
        manager = self.get_index_manager()
        manager.delete_remote_index()
        manager.delete_files(remote_files_to_remove)

        self.openai_vector_store_id = ""
        self.save(update_fields=["openai_vector_store_id"])

    def get_index_manager(self):
        if self.is_index and self.is_remote_index:
            return self.llm_provider.get_remote_index_manager(self.openai_vector_store_id)
        else:
            return self.llm_provider.get_local_index_manager(embedding_model_name=self.embedding_provider_model.name)

    def get_query_vector(self, query: str) -> list[float]:
        """Get the embedding vector for a query using the embedding provider model"""
        if not self.embedding_provider_model:
            raise IndexConfigurationException("Embedding provider model is missing this collection")

        index_manager = self.get_index_manager()
        return index_manager.get_embedding_vector(query)

    def get_search_tool(self, max_results: int, generate_citations: bool = True) -> OpenAIBuiltinTool | SearchIndexTool:
        """
        Returns either the tool configuration. If the collection is a remote index, it returns the builtin file search
        tool, otherwise it returns a SearchIndexTool.
        """
        if not self.is_index:
            raise IndexConfigurationException("Non-indexed collections do not have search tools")

        if self.is_remote_index:
            return OpenAIBuiltinTool(
                type="file_search",
                vector_store_ids=[self.openai_vector_store_id],
                max_num_results=max_results,
            )

        search_config = SearchToolConfig(
            index_id=self.id, max_results=max_results, generate_citations=generate_citations
        )
        return SearchIndexTool(search_config=search_config)

    def add_files_to_index(
        self,
        collection_files: Iterator[CollectionFile],
        chunk_size: int = None,
        chunk_overlap: int = None,
    ):
        index_manager = self.get_index_manager()
        index_manager.add_files(collection_files, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    def ensure_remote_index_created(self, file_ids: list[str] = None):
        """
        Ensure that the remote index is created for this collection if it is not already created.
        This is used when the collection is created or when the version is created.
        """
        if not self.is_remote_index or self.openai_vector_store_id:
            return

        file_ids = file_ids or []
        self.openai_vector_store_id = self.llm_provider.create_remote_index(name=self.index_name, file_ids=file_ids)
        self.save(update_fields=["openai_vector_store_id"])
