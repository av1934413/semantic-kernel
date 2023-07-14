# Copyright (c) Microsoft. All rights reserved.

import json
import os
import uuid
from datetime import datetime
from logging import Logger
from typing import List, Optional, Tuple

from azure.core.credentials import AzureKeyCredential, TokenCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import (
    PrioritizedFields,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticSettings,
    VectorSearch,
    VectorSearchAlgorithmConfiguration,
)
from azure.search.documents.models import Vector
from dotenv import load_dotenv
from numpy import ndarray

from semantic_kernel.connectors.memory.azure_cog_search.acs_utils import (
    acs_field_selection,
    acs_schema,
    convert_to_memory_record,
    create_credentials,
)
from semantic_kernel.memory.memory_record import MemoryRecord
from semantic_kernel.memory.memory_store_base import MemoryStoreBase
from semantic_kernel.utils.null_logger import NullLogger


class CognitiveSearchMemoryStore(MemoryStoreBase):
    _cogsearch_indexclient: SearchIndexClient
    _cogsearch_records: list
    _cogsearch_creds: AzureKeyCredential
    _cogsearch_token_creds: TokenCredential
    _logger: Logger

    def __init__(
        self,
        acs_endpoint: str,
        acs_credential: Optional[AzureKeyCredential] = None,
        acs_search_key: Optional[str] = None,
        acs_token_credential: Optional[TokenCredential] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        """Initializes a new instance of the CogintiveSearchMemoryStore class.

        Arguments:
            endpoint {str} -- The endpoint of the CogintiveSearch service.
            logger {Optional[Logger]} -- The logger to use. (default: {None})
        """
        try:
            from azure.search.documents.indexes import SearchIndexClient
        except ImportError:
            raise ValueError(
                "Error: Unable to import azure cognitive search client python package."
                "Please install azure cognitive search client"
            )

        # Configure environment variables
        load_dotenv()

        # Add key or rbac credentials
        if acs_credential:
            self._cogsearch_creds = acs_credential
        elif acs_token_credential:
            self._cogsearch_token_creds = acs_token_credential
        else:
            self._cogsearch_creds = create_credentials(
                use_async=True, azsearch_api_key=acs_search_key
            )

        if not self._cogsearch_creds or not self._cogsearch_token_creds:
            raise ValueError(
                "Error: Unable to create azure cognitive search client credentials."
            )

        # Get service endpoint from environment variable
        service_endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")

        # Override environment variable with endpoint provided
        if acs_endpoint:
            service_endpoint = acs_endpoint

        if not service_endpoint:
            raise ValueError(
                "Error: A valid Azure Cognitive Search client endpoint is required."
            )

        self._cogsearch_indexclient = SearchIndexClient(
            endpoint=service_endpoint, credential=self._cogsearch_creds
        )

        self._logger = logger or NullLogger()

    async def create_collection_async(
        self,
        collection_name: str,
        vector_size: int,
        vector_config: Optional[VectorSearchAlgorithmConfiguration],
        semantic_config: Optional[SemanticConfiguration],
    ) -> None:
        """Creates a new collection if it does not exist.

        Arguments:
            collection_name {str} -- The name of the collection to create.
            vector_size {int} -- The size of the vector.
        Returns:
            None
        """

        ## Covert complex type to json string - Utils.py

        if vector_config:
            vector_search = VectorSearch(algorithm_configurations=[vector_config])
        else:
            vector_search = VectorSearch(
                algorithm_configurations=[
                    VectorSearchAlgorithmConfiguration(
                        name="az-vector-config",
                        kind="hnsw",
                        hnsw_parameters={
                            "m": 4,
                            "efConstruction": 400,
                            "efSearch": 500,
                            "metric": "cosine",
                        },
                    )
                ]
            )

        # Check to see if collection exists
        collection_index = await self._cogsearch_indexclient.get_index(collection_name)

        if not collection_index:
            ## Look to see if semantic config is there or use default below
            if not semantic_config:
                semantic_config = SemanticConfiguration(
                    name="az-semantic-config",
                    prioritized_fields=PrioritizedFields(
                        title_field=SemanticField(field_name="vector_id"),
                    ),
                )

            # Create the semantic settings with the configuration
            semantic_settings = SemanticSettings(configurations=[semantic_config])

            # Create the search index with the semantic settings
            index = SearchIndex(
                name=collection_name,
                fields=acs_schema(vector_size),
                vector_search=vector_search,
                semantic_settings=semantic_settings,
            )

            await self._cogsearch_indexclient.create_index(index)

    async def get_collections_async(
        self,
    ) -> List[str]:
        """Gets the list of collections.

        Returns:
            List[str] -- The list of collections.
        """
        results_list = list(str)
        items = self._cogsearch_indexclient.list_index_names()

        for result in items:
            results_list.append(result)

        return results_list

    async def get_collection(self, collection_name: str) -> SearchIndex:
        """Gets the a collections based upon collection name.

        Returns:
            SearchIndex -- Collection Information from ACS about collection.
        """
        collection_result = await self._cogsearch_indexclient.get_index(
            name=collection_name
        )

        return collection_result

    async def delete_collection_async(self, collection_name: str) -> None:
        """Deletes a collection.

        Arguments:
            collection_name {str} -- The name of the collection to delete.

        Returns:
            None
        """
        self._cogsearch_indexclient.delete_index(index=collection_name)

    async def does_collection_exist_async(self, collection_name: str) -> bool:
        """Checks if a collection exists.

        Arguments:
            collection_name {str} -- The name of the collection to check.

        Returns:
            bool -- True if the collection exists; otherwise, False.
        """

        collection_result = await self._cogsearch_indexclient.get_index(
            name=collection_name
        )

        if collection_result:
            return True
        else:
            return False

    async def upsert_async(self, collection_name: str, record: MemoryRecord) -> str:
        """Upserts a record.

        Arguments:
            collection_name {str} -- The name of the collection to upsert the record into.
            record {MemoryRecord} -- The record to upsert.

        Returns:
            str -- The unique database key of the record.
        """

        ## Look up Search client class to see if exists or create
        acs_search_client = self._cogsearch_indexclient.get_search_client(
            collection_name
        )

        # If no Search client exists, create one
        if not acs_search_client:
            if self._cogsearch_creds:
                acs_search_client = SearchClient(
                    service_endpoint=self._cogsearch_indexclient._endpoint,
                    index_name=collection_name,
                    credential=self._cogsearch_creds,
                )

        if record._id:
            id = record._id
        else:
            id = uuid.uuid4()  ## Check to make sure string

        acs_doc = {
            "timestamp": datetime.utcnow(),
            "vector_id": str(id),
            "payload": str(json.load(record._payload)),
            "vector": record._embedding.tolist(),
            "additional_metadata": record._additional_metadata,
        }

        result = acs_search_client.upload_documents(documents=[acs_doc])

        ## Throw exception if problem
        ## Clean this up not needed if throwing
        if result[0].succeeded:
            return id
        else:
            raise ValueError("Error: Unable to upsert record.")

    async def upsert_batch_async(
        self, collection_name: str, records: List[MemoryRecord]
    ) -> List[str]:
        """Upserts a batch of records.

        Arguments:
            collection_name {str} -- The name of the collection to upsert the records into.
            records {List[MemoryRecord]} -- The records to upsert.

        Returns:
            List[str] -- The unique database keys of the records.
        """
        ## Initialize search client here
        ## Look up Search client class to see if exists or create
        acs_search_client = self._cogsearch_indexclient.get_search_client(
            collection_name
        )

        # If no Search client exists, create one
        if not acs_search_client:
            if self._cogsearch_creds:
                acs_search_client = SearchClient(
                    service_endpoint=self._cogsearch_indexclient._endpoint,
                    index_name=collection_name,
                    credential=AzureKeyCredential(self._cogsearch_creds),
                )

        acs_documents = []
        acs_ids = []

        for record in records:
            if record._id:
                id = record._id
            else:
                id = uuid.uuid4()

            acs_ids.append(id)

            acs_doc = {
                "timestamp": datetime.utcnow().timestamp(),
                "vector_id": str(id),
                "payload": str(json.load(record._text)),
                "vector": record._embedding.tolist(),
                "additional_metadata": record._additional_metadata,
            }

            acs_documents.append(acs_doc)

        result = acs_search_client.upload_documents(documents=[acs_doc])

        if result[0].succeeded:
            return acs_ids
        else:
            return None

    async def get_async(
        self, collection_name: str, key: str, with_embedding: bool = True
    ) -> MemoryRecord:
        """Gets a record.

        Arguments:
            collection_name {str} -- The name of the collection to get the record from.
            key {str} -- The unique database key of the record.
            with_embedding {bool} -- Whether to include the embedding in the result. (default: {False})

        Returns:
            MemoryRecord -- The record.
        """

        ## Look up Search client class to see if exists or create
        acs_search_client = self._cogsearch_indexclient.get_search_client(
            collection_name
        )

        ## If no Search client exists, create one
        if not acs_search_client:
            if self._cogsearch_creds:
                acs_search_client = SearchClient(
                    service_endpoint=self._cogsearch_indexclient._endpoint,
                    index_name=collection_name,
                    credential=AzureKeyCredential(self._cogsearch_creds),
                )

        search_id = key

        ## Get document using key (search_id)
        acs_result = await acs_search_client.get_document(key=search_id)

        ## Create Memory record from document
        acs_mem_record = convert_to_memory_record(acs_result)

        return acs_mem_record

    async def get_batch_async(
        self, collection_name: str, keys: List[str], with_embeddings: bool = False
    ) -> List[MemoryRecord]:
        """Gets a batch of records.

        Arguments:
            collection_name {str} -- The name of the collection to get the records from.
            keys {List[str]} -- The unique database keys of the records.
            with_embeddings {bool} -- Whether to include the embeddings in the results. (default: {False})

        Returns:
            List[MemoryRecord] -- The records.
        """

        acs_results = List[MemoryRecord]

        for acs_key in keys:
            ## TODO: Call get_document in loop
            result = await self.get_async(collection_name=collection_name, key=acs_key)
            acs_results.append(result)

        return acs_results

    async def remove_async(self, collection_name: str, key: str) -> None:
        """Removes a record.

        Arguments:
            collection_name {str} -- The name of the collection to remove the record from.
            key {str} -- The unique database key of the record to remove.

        Returns:
            None
        """

        ## Look up Search client class to see if exists or create
        acs_search_client = self._cogsearch_indexclient.get_search_client(
            collection_name
        )

        ## If no Search client exists, create one
        if not acs_search_client:
            if self._cogsearch_creds:
                acs_search_client = SearchClient(
                    service_endpoint=self._cogsearch_indexclient._endpoint,
                    index_name=collection_name,
                    credential=AzureKeyCredential(self._cogsearch_creds),
                )

        acs_data = {
            "vector_id": key,
        }

        acs_search_client.delete_documents(documents=[acs_data])

        ## TODO make key list
        await self.remove_batch_async(self, collection_name, key)

    async def remove_batch_async(self, collection_name: str, keys: List[str]) -> None:
        """Removes a batch of records.

        Arguments:
            collection_name {str} -- The name of the collection to remove the records from.
            keys {List[str]} -- The unique database keys of the records to remove.

        Returns:
            None
        """

        ## TODO: call delete_documents API pass list of dicts
        for acs_key in keys:
            self.remove_async(collection_name=collection_name, key=acs_key)

    async def get_nearest_match_async(
        self,
        collection_name: str,
        embedding: ndarray,
        limit: int = 1,
        min_relevance_score: float = 0.0,
        with_embedding: bool = False,
    ) -> Tuple[MemoryRecord, float]:
        """Gets the nearest match to an embedding using vector configuration parameters.

        Arguments:
            collection_name {str} -- The name of the collection to get the nearest match from.
            embedding {ndarray} -- The embedding to find the nearest match to.
            min_relevance_score {float} -- The minimum relevance score of the match. (default: {0.0})
            with_embedding {bool} -- Whether to include the embedding in the result. (default: {False})

        Returns:
            Tuple[MemoryRecord, float] -- The record and the relevance score.
        """

        ## Look up Search client class to see if exists or create
        acs_search_client = self._cogsearch_indexclient.get_search_client(
            collection_name
        )

        # If no Search client exists, create one
        if not acs_search_client:
            if self._cogsearch_creds:
                acs_search_client = SearchClient(
                    service_endpoint=self._cogsearch_indexclient._endpoint,
                    index_name=collection_name,
                    credential=AzureKeyCredential(self._cogsearch_creds),
                )

        selection_fields = acs_field_selection(include_embedding=with_embedding)

        acs_result = await acs_search_client.search(
            vector=Vector(value=embedding, k=limit, fields="vector"),
            select=selection_fields,
            top=limit,
        )

        # Convert to MemoryRecord
        vector_result = convert_to_memory_record(acs_result)

        return tuple(
            vector_result, acs_result["score"]
        )  ## How do I get score? Do I need to manually calculate?

    async def get_nearest_matches_async(
        self,
        collection_name: str,
        embedding: ndarray,
        limit: int,
        min_relevance_score: float = 0.0,
        with_embeddings: bool = False,
    ) -> List[Tuple[MemoryRecord, float]]:
        """Gets the nearest matches to an embedding using vector configuration.

        Arguments:
            collection_name {str} -- The name of the collection to get the nearest matches from.
            embedding {ndarray} -- The embedding to find the nearest matches to.
            limit {int} -- The maximum number of matches to return.
            min_relevance_score {float} -- The minimum relevance score of the matches. (default: {0.0})
            with_embeddings {bool} -- Whether to include the embeddings in the results. (default: {False})

        Returns:
            List[Tuple[MemoryRecord, float]] -- The records and their relevance scores.
        """

        ## Look up Search client class to see if exists or create
        acs_search_client = self._cogsearch_indexclient.get_search_client(
            collection_name
        )

        # If no Search client exists, create one
        if not acs_search_client:
            if self._cogsearch_creds:
                acs_search_client = SearchClient(
                    service_endpoint=self._cogsearch_indexclient._endpoint,
                    index_name=collection_name,
                    credential=AzureKeyCredential(self._cogsearch_creds),
                )

        if with_embeddings:
            select_fields = ["vector_id", "vector", "payload", "additional_metadata"]
        else:
            select_fields = ["vector_id", "payload", "additional_metadata"]

        results = await acs_search_client.search(
            vector=Vector(value=embedding, k=limit, fields="vector"),
            select=select_fields,
            top=limit,
        )

        nearest_results = []

        # Convert the results to MemoryRecords
        ## TODO: Update call if withembeddings is false
        for acs_result in results:
            vector_result = convert_to_memory_record(acs_result)

            nearest_results.append(
                tuple(vector_result, acs_result["score"])
            )  ## How do I get score? Do I need to manually calculate?

        return nearest_results
