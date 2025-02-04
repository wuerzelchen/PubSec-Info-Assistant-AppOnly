# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from io import StringIO
from typing import Optional
from datetime import datetime
import asyncio
import logging
import os
import json
import urllib.parse
import pandas as pd
import pydantic
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, Form
from fastapi.responses import RedirectResponse, StreamingResponse
import openai
from approaches.comparewebwithwork import CompareWebWithWork
from approaches.compareworkwithweb import CompareWorkWithWeb
from approaches.chatreadretrieveread import ChatReadRetrieveReadApproach
from approaches.chatwebretrieveread import ChatWebRetrieveRead
from approaches.gpt_direct_approach import GPTDirectApproach
from approaches.approach import Approaches
from azure.identity import ManagedIdentityCredential, AzureAuthorityHosts, DefaultAzureCredential, get_bearer_token_provider
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from azure.search.documents import SearchClient
from azure.storage.blob import BlobServiceClient, ContentSettings

# === ENV Setup ===

ENV = {
    "AZURE_SEARCH_SERVICE": "gptkb",
    "AZURE_SEARCH_SERVICE_ENDPOINT": None,
    "AZURE_SEARCH_INDEX": "gptkbindex",
    "AZURE_SEARCH_AUDIENCE": None,
    "USE_SEMANTIC_RERANKER": "true",
    "AZURE_OPENAI_SERVICE": "myopenai",
    "AZURE_OPENAI_RESOURCE_GROUP": "",
    "AZURE_OPENAI_ENDPOINT": "",
    "AZURE_OPENAI_AUTHORITY_HOST": "AzureCloud",
    "AZURE_OPENAI_CHATGPT_DEPLOYMENT": "gpt-35-turbo-16k",
    "AZURE_OPENAI_CHATGPT_MODEL_NAME": "",
    "AZURE_OPENAI_CHATGPT_MODEL_VERSION": "",
    "USE_AZURE_OPENAI_EMBEDDINGS": "false",
    "EMBEDDING_DEPLOYMENT_NAME": "",
    "AZURE_OPENAI_EMBEDDINGS_MODEL_NAME": "",
    "AZURE_OPENAI_EMBEDDINGS_VERSION": "",
    "AZURE_SUBSCRIPTION_ID": None,
    "AZURE_ARM_MANAGEMENT_API": "https://management.azure.com",
    "CHAT_WARNING_BANNER_TEXT": "",
    "APPLICATION_TITLE": "Information Assistant, built with Azure OpenAI",
    "KB_FIELDS_CONTENT": "content",
    "KB_FIELDS_PAGENUMBER": "pages",
    "KB_FIELDS_SOURCEFILE": "file_name",
    "KB_FIELDS_CHUNKFILE": "chunk_file",
    "QUERY_TERM_LANGUAGE": "English",
    "TARGET_EMBEDDINGS_MODEL": "BAAI/bge-small-en-v1.5",
    "ENRICHMENT_APPSERVICE_URL": "enrichment",
    "TARGET_TRANSLATION_LANGUAGE": "en",
    "AZURE_AI_ENDPOINT": None,
    "AZURE_AI_LOCATION": "",
    "BING_SEARCH_ENDPOINT": "https://api.bing.microsoft.com/",
    "BING_SEARCH_KEY": "",
    "ENABLE_BING_SAFE_SEARCH": "true",
    "ENABLE_WEB_CHAT": "false",
    "ENABLE_UNGROUNDED_CHAT": "false",
    "LOCAL_DEBUG": "false",
    "AZURE_AI_CREDENTIAL_DOMAIN": "cognitiveservices.azure.com",
    "AZURE_BLOB_STORAGE_ENDPOINT": None,
    "AZURE_BLOB_STORAGE_CONTAINER": "files",
}

for key, value in ENV.items():
    new_value = os.getenv(key)
    if new_value is not None:
        ENV[key] = new_value
    elif value is None:
        raise ValueError(f"Environment variable {key} not set")

str_to_bool = {'true': True, 'false': False}

log = logging.getLogger("uvicorn")
log.setLevel('DEBUG')
log.propagate = True


class StatusResponse(pydantic.BaseModel):
    """The response model for the health check endpoint"""
    status: str
    uptime_seconds: float
    version: str


start_time = datetime.now()

IS_READY = False

DF_FINAL = None
# Used by the OpenAI SDK
openai.api_type = "azure"
openai.api_base = ENV["AZURE_OPENAI_ENDPOINT"]
if ENV["AZURE_OPENAI_AUTHORITY_HOST"] == "AzureUSGovernment":
    AUTHORITY = AzureAuthorityHosts.AZURE_GOVERNMENT
else:
    AUTHORITY = AzureAuthorityHosts.AZURE_PUBLIC_CLOUD
openai.api_version = "2024-02-01"
# When debugging in VSCode, use the current user identity to authenticate with Azure OpenAI,
# Cognitive Search and Blob Storage (no secrets needed, just use 'az login' locally)
# Use managed identity when deployed on Azure.
# If you encounter a blocking error during a DefaultAzureCredntial resolution, you can exclude
# the problematic credential by using a parameter (ex. exclude_shared_token_cache_credential=True)
if ENV["LOCAL_DEBUG"] == "true":
    azure_credential = DefaultAzureCredential(authority=AUTHORITY)
else:
    azure_credential = ManagedIdentityCredential(authority=AUTHORITY)
# Comment these two lines out if using keys, set your API key in the OPENAI_API_KEY
# environment variable instead
openai.api_type = "azure_ad"
token_provider = get_bearer_token_provider(azure_credential,
                                           f'https://{ENV["AZURE_AI_CREDENTIAL_DOMAIN"]}/.default')
openai.azure_ad_token_provider = token_provider
# openai.api_key = ENV["AZURE_OPENAI_SERVICE_KEY"]

# Set up clients for Cognitive Search and Storage
search_client = SearchClient(
    endpoint=ENV["AZURE_SEARCH_SERVICE_ENDPOINT"],
    index_name=ENV["AZURE_SEARCH_INDEX"],
    credential=azure_credential
    # audience=ENV["AZURE_SEARCH_AUDIENCE"]
)
blob_client = BlobServiceClient(
    account_url=ENV["AZURE_BLOB_STORAGE_ENDPOINT"],
    credential=azure_credential,
)
blob_container = blob_client.get_container_client(
    ENV["AZURE_BLOB_STORAGE_CONTAINER"])

MODEL_NAME = ''
MODEL_VERSION = ''

# Set up OpenAI management client
openai_mgmt_client = CognitiveServicesManagementClient(
    credential=azure_credential,
    subscription_id=ENV["AZURE_SUBSCRIPTION_ID"],
    base_url=ENV["AZURE_ARM_MANAGEMENT_API"],
    credential_scopes=[ENV["AZURE_ARM_MANAGEMENT_API"] + "/.default"])

deployment = openai_mgmt_client.deployments.get(
    resource_group_name=ENV["AZURE_OPENAI_RESOURCE_GROUP"],
    account_name=ENV["AZURE_OPENAI_SERVICE"],
    deployment_name=ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"])

MODEL_NAME = deployment.properties.model.name
MODEL_VERSION = deployment.properties.model.version

if str_to_bool.get(ENV["USE_AZURE_OPENAI_EMBEDDINGS"]):
    embedding_deployment = openai_mgmt_client.deployments.get(
        resource_group_name=ENV["AZURE_OPENAI_RESOURCE_GROUP"],
        account_name=ENV["AZURE_OPENAI_SERVICE"],
        deployment_name=ENV["EMBEDDING_DEPLOYMENT_NAME"])

    EMBEDDING_MODEL_NAME = embedding_deployment.properties.model.name
    EMBEDDING_MODEL_VERSION = embedding_deployment.properties.model.version
else:
    EMBEDDING_MODEL_NAME = ""
    EMBEDDING_MODEL_VERSION = ""

chat_approaches = {
    Approaches.ReadRetrieveRead: ChatReadRetrieveReadApproach(
        search_client,
        ENV["AZURE_OPENAI_ENDPOINT"],
        ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        ENV["KB_FIELDS_SOURCEFILE"],
        ENV["KB_FIELDS_CONTENT"],
        ENV["KB_FIELDS_PAGENUMBER"],
        ENV["KB_FIELDS_CHUNKFILE"],
        ENV["AZURE_BLOB_STORAGE_CONTAINER"],
        blob_client,
        ENV["QUERY_TERM_LANGUAGE"],
        MODEL_NAME,
        MODEL_VERSION,
        ENV["TARGET_EMBEDDINGS_MODEL"],
        ENV["ENRICHMENT_APPSERVICE_URL"],
        ENV["TARGET_TRANSLATION_LANGUAGE"],
        ENV["AZURE_AI_ENDPOINT"],
        ENV["AZURE_AI_LOCATION"],
        token_provider,
        str_to_bool.get(ENV["USE_SEMANTIC_RERANKER"])
    ),
    Approaches.ChatWebRetrieveRead: ChatWebRetrieveRead(
        MODEL_NAME,
        ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        ENV["TARGET_TRANSLATION_LANGUAGE"],
        ENV["BING_SEARCH_ENDPOINT"],
        ENV["BING_SEARCH_KEY"],
        str_to_bool.get(ENV["ENABLE_BING_SAFE_SEARCH"]),
        ENV["AZURE_OPENAI_ENDPOINT"],
        token_provider
    ),
    Approaches.CompareWorkWithWeb: CompareWorkWithWeb(
        MODEL_NAME,
        ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        ENV["TARGET_TRANSLATION_LANGUAGE"],
        ENV["BING_SEARCH_ENDPOINT"],
        ENV["BING_SEARCH_KEY"],
        str_to_bool.get(ENV["ENABLE_BING_SAFE_SEARCH"]),
        ENV["AZURE_OPENAI_ENDPOINT"],
        token_provider
    ),
    Approaches.CompareWebWithWork: CompareWebWithWork(
        search_client,
        ENV["AZURE_OPENAI_ENDPOINT"],
        ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        ENV["KB_FIELDS_SOURCEFILE"],
        ENV["KB_FIELDS_CONTENT"],
        ENV["KB_FIELDS_PAGENUMBER"],
        ENV["KB_FIELDS_CHUNKFILE"],
        ENV["AZURE_BLOB_STORAGE_CONTAINER"],
        blob_client,
        ENV["QUERY_TERM_LANGUAGE"],
        MODEL_NAME,
        MODEL_VERSION,
        ENV["TARGET_EMBEDDINGS_MODEL"],
        ENV["ENRICHMENT_APPSERVICE_URL"],
        ENV["TARGET_TRANSLATION_LANGUAGE"],
        ENV["AZURE_AI_ENDPOINT"],
        ENV["AZURE_AI_LOCATION"],
        token_provider,
        str_to_bool.get(ENV["USE_SEMANTIC_RERANKER"])
    ),
    Approaches.GPTDirect: GPTDirectApproach(
        token_provider,
        ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        ENV["QUERY_TERM_LANGUAGE"],
        MODEL_NAME,
        MODEL_VERSION,
        ENV["AZURE_OPENAI_ENDPOINT"]
    )
}

IS_READY = True

# Create API
app = FastAPI(
    title="IA Web API",
    description="A Python API to serve as Backend For the Information Assistant Web App",
    version="0.1.0",
    docs_url="/docs",
)


@app.get("/", include_in_schema=False, response_class=RedirectResponse)
async def root():
    """Redirect to the index.html page"""
    return RedirectResponse(url="/index.html")


@app.get("/health", response_model=StatusResponse, tags=["health"])
def health():
    """Returns the health of the API

    Returns:
        StatusResponse: The health of the API
    """

    uptime = datetime.now() - start_time
    uptime_seconds = uptime.total_seconds()

    output = {"status": None, "uptime_seconds": uptime_seconds,
              "version": app.version}

    if IS_READY:
        output["status"] = "ready"
    else:
        output["status"] = "loading"

    return output


@app.post("/chat")
async def chat(request: Request):
    """Chat with the bot using a given approach

    Args:
        request (Request): The incoming request object

    Returns:
        dict: The response containing the chat results

    Raises:
        dict: The error response if an exception occurs during the chat
    """
    json_body = await request.json()
    approach = json_body.get("approach")
    try:
        impl = chat_approaches.get(Approaches(int(approach)))
        if not impl:
            return {"error": "unknown approach"}, 400
        if (Approaches(int(approach)) == Approaches.CompareWorkWithWeb or
                Approaches(int(approach)) == Approaches.CompareWebWithWork):
            r = impl.run(json_body.get("history", []),
                         json_body.get("overrides", {}),
                         json_body.get("citation_lookup", {}),
                         json_body.get("thought_chain", {}))
        else:
            r = impl.run(json_body.get("history", []),
                         json_body.get("overrides", {}),
                         {},
                         json_body.get("thought_chain", {}))

        return StreamingResponse(r, media_type="application/x-ndjson")

    except Exception as ex:
        log.error("Error in chat:: %s", ex)
        raise HTTPException(status_code=500, detail=str(ex)) from ex


@app.post("/getfolders")
async def get_folders():
    """
    Get all folders.

    Parameters:
    - request: The HTTP request object.

    Returns:
    - results: list of unique folders.
    """
    try:
        # Initialize an empty list to hold the folder paths
        folders = []
        # List all blobs in the container
        blob_list = blob_container.list_blobs()
        # Iterate through the blobs and extract folder names and add unique values to the list
        for blob in blob_list:
            # Extract the folder path if exists
            folder_path = os.path.dirname(blob.name)
            if folder_path and folder_path not in folders:
                folders.append(folder_path)
    except Exception as ex:
        log.exception("Exception in /getfolders")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return folders


@app.get("/getInfoData")
async def get_info_data():
    """
    Get the info data for the app.

    Returns:
        dict: A dictionary containing various information data for the app.
            - "AZURE_OPENAI_CHATGPT_DEPLOYMENT": The deployment information for Azure OpenAI ChatGPT.
            - "AZURE_OPENAI_MODEL_NAME": The name of the Azure OpenAI model.
            - "AZURE_OPENAI_MODEL_VERSION": The version of the Azure OpenAI model.
            - "AZURE_OPENAI_SERVICE": The Azure OpenAI service information.
            - "AZURE_SEARCH_SERVICE": The Azure search service information.
            - "AZURE_SEARCH_INDEX": The Azure search index information.
            - "TARGET_LANGUAGE": The target language for query terms.
            - "USE_AZURE_OPENAI_EMBEDDINGS": Flag indicating whether to use Azure OpenAI embeddings.
            - "EMBEDDINGS_DEPLOYMENT": The deployment information for embeddings.
            - "EMBEDDINGS_MODEL_NAME": The name of the embeddings model.
            - "EMBEDDINGS_MODEL_VERSION": The version of the embeddings model.
    """
    response = {
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT": ENV["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        "AZURE_OPENAI_MODEL_NAME": f"{MODEL_NAME}",
        "AZURE_OPENAI_MODEL_VERSION": f"{MODEL_VERSION}",
        "AZURE_OPENAI_SERVICE": ENV["AZURE_OPENAI_SERVICE"],
        "AZURE_SEARCH_SERVICE": ENV["AZURE_SEARCH_SERVICE"],
        "AZURE_SEARCH_INDEX": ENV["AZURE_SEARCH_INDEX"],
        "TARGET_LANGUAGE": ENV["QUERY_TERM_LANGUAGE"],
        "USE_AZURE_OPENAI_EMBEDDINGS": ENV["USE_AZURE_OPENAI_EMBEDDINGS"],
        "EMBEDDINGS_DEPLOYMENT": ENV["EMBEDDING_DEPLOYMENT_NAME"],
        "EMBEDDINGS_MODEL_NAME": f"{EMBEDDING_MODEL_NAME}",
        "EMBEDDINGS_MODEL_VERSION": f"{EMBEDDING_MODEL_VERSION}",
    }
    return response


@app.get("/getWarningBanner")
async def get_warning_banner():
    """Get the warning banner text"""
    response = {
        "WARNING_BANNER_TEXT": ENV["CHAT_WARNING_BANNER_TEXT"]
    }
    return response


@app.get("/getMaxCSVFileSize")
async def get_max_csv_file_size():
    """Get the max csv size"""
    response = {
        "MAX_CSV_FILE_SIZE": ENV["MAX_CSV_FILE_SIZE"]
    }
    return response


@app.post("/getcitation")
async def get_citation(request: Request):
    """
    Get the citation for a given file

    Parameters:
        request (Request): The HTTP request object

    Returns:
        dict: The citation results in JSON format
    """
    try:
        json_body = await request.json()
        citation = urllib.parse.unquote(json_body.get("citation"))
        blob = blob_container.get_blob_client(citation).download_blob()
        decoded_text = blob.readall().decode()
        results = json.loads(decoded_text)
    except Exception as ex:
        log.exception("Exception in /getcitation")
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return results

# Return APPLICATION_TITLE


@app.get("/getApplicationTitle")
async def get_application_title():
    """Get the application title text

    Returns:
        dict: A dictionary containing the application title.
    """
    response = {
        "APPLICATION_TITLE": ENV["APPLICATION_TITLE"]
    }
    return response


@app.get("/getFeatureFlags")
async def get_feature_flags():
    """
    Get the feature flag settings for the app.

    Returns:
        dict: A dictionary containing various feature flags for the app.
            - "ENABLE_WEB_CHAT": Flag indicating whether web chat is enabled.
            - "ENABLE_UNGROUNDED_CHAT": Flag indicating whether ungrounded chat is enabled.
            - "ENABLE_MATH_ASSISTANT": Flag indicating whether the math assistant is enabled.
            - "ENABLE_TABULAR_DATA_ASSISTANT": Flag indicating whether the tabular data assistant is enabled.
    """
    response = {
        "ENABLE_WEB_CHAT": str_to_bool.get(ENV["ENABLE_WEB_CHAT"]),
        "ENABLE_UNGROUNDED_CHAT": str_to_bool.get(ENV["ENABLE_UNGROUNDED_CHAT"]),
    }
    return response


@app.post("/get-file")
async def get_file(request: Request):
    data = await request.json()
    file_path = data['path']

    # Extract container name and blob name from the file path
    container_name, blob_name = file_path.split('/', 1)

    # Download the blob to a local file

    citation_blob_client = blob_container.get_blob_client(blob=blob_name)
    stream = citation_blob_client.download_blob().chunks()
    blob_properties = citation_blob_client.get_blob_properties()

    return StreamingResponse(stream,
                             media_type=blob_properties.content_settings.content_type,
                             headers={"Content-Disposition": f"inline; filename={blob_name}"})

app.mount("/", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    log.info("IA WebApp Starting Up...")
