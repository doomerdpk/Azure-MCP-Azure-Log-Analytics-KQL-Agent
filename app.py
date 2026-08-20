import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import streamlit as st
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import CredentialUnavailableError

from openai import AzureOpenAI
from azure_mcp import get_table_schema, list_log_tables, run_kql_query

logger = logging.getLogger(__name__)

selection_tools = [
    {
        "type": "function",
        "function": {
            "name": "select_tables",
            "description": "Select the relevant tables from the supplied workspace table list",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One or more table names from the supplied list"
                    }
                },
                "required": ["table_names"]
            }
        }
    }
]

query_tools = [
    {
        "type": "function",
        "function": {
            "name": "run_kql_query",
            "description": "Execute a KQL query against Azure Log Analytics",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "duration": {
                        "type": "string",
                        "description": "Number plus m, h, d, or w, for example 30m, 12h, 7d, or 2w"
                    }
                },
                "required": ["query"]
            }
        }
    }
]

MAX_RESULT_ROWS = 500
MAX_CONTEXT_MESSAGES = 6


@st.cache_resource
def get_openai_client():
    return AzureOpenAI(
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
    )


def azure_failure_message(action, error):
    if isinstance(error, (CredentialUnavailableError, ClientAuthenticationError)):
        return f"Azure authentication failed while {action}. Check your Azure identity and permissions."
    if isinstance(error, HttpResponseError):
        return f"Azure returned an error while {action}. Check the workspace ID, query, and Azure permissions."
    return f"I could not {action}. Check the application logs for details."


def compact_schemas(schemas):
    compacted = {}
    for table_name, columns in schemas.items():
        compacted[table_name] = [
            {
                "name": column.get("ColumnName") or column.get("Name") or column.get("name"),
                "type": column.get("ColumnType") or column.get("DataType") or column.get("Type") or column.get("type"),
            }
            for column in columns
            if isinstance(column, dict)
            and (column.get("ColumnName") or column.get("Name") or column.get("name"))
        ]
    return compacted


def build_result_summary(question, query, results):
    result_context = json.dumps(results[:100], default=str)
    if len(result_context) > 30_000:
        result_context = result_context[:30_000] + "\n[Results truncated]"

    try:
        response = create_completion_with_retry(
            model="gpt-5-mini",
            messages=[
                {
                    "role": "system",
                    "content": """
You are an Azure Log Analytics analyst. Summarize the findings in the supplied query results for the user.
Base every claim only on the supplied data. Explain the most important patterns, counts, errors, trends, or anomalies
that answer the question. Mention when the data is insufficient to support a conclusion. End with one practical
recommendation when appropriate. Be concise and do not describe your role or the summarization process.
"""
                },
                {
                    "role": "user",
                    "content": (
                        f"User question:\n{question}\n\n"
                        f"Generated KQL:\n{query}\n\n"
                        f"Query results (up to the first 100 rows):\n{result_context}"
                    )
                }
            ],
        )
        summary = response.choices[0].message.content
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        raise ValueError("summary response was empty")
    except Exception:
        logger.exception("Result-summary model call failed")
        return "I retrieved the query results, but I could not generate a findings summary. Review the raw results below."


def validate_generated_kql(query, selected_tables):
    """Validate model-generated KQL before sending it to Log Analytics."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("The generated query is empty.")

    normalized_query = query.strip().rstrip(";").strip()
    if len(normalized_query) > 10_000 or ";" in normalized_query:
        raise ValueError("The generated query is too long or contains multiple statements.")

    if re.search(
        r"\.(drop|delete|set|alter|create|rename|move|clear|ingest)\b",
        normalized_query,
        re.IGNORECASE,
    ):
        raise ValueError("Only read-only KQL queries are allowed.")

    source_names = re.findall(
        r"(?:^|\bunion\s+)([A-Za-z_][A-Za-z0-9_]*)",
        normalized_query,
        re.IGNORECASE,
    )
    unknown_sources = {
        source for source in source_names
        if source.lower() not in {table.lower() for table in selected_tables}
    }
    if unknown_sources:
        raise ValueError(
            "The query references tables that were not selected: "
            + ", ".join(sorted(unknown_sources))
        )

    if not re.search(
        r"\bago\s*\(|\bTimeGenerated\b\s*(?:>|>=|between)",
        normalized_query,
        re.IGNORECASE,
    ):
        raise ValueError("The query must include a time filter.")

    limit_match = re.search(r"\|\s*(?:take|limit)\s+(\d+)\b", normalized_query, re.IGNORECASE)
    if limit_match and int(limit_match.group(1)) > MAX_RESULT_ROWS:
        raise ValueError(f"The query result limit cannot exceed {MAX_RESULT_ROWS} rows.")
    if not limit_match:
        normalized_query += f"\n| take {MAX_RESULT_ROWS}"

    return normalized_query


def create_completion_with_retry(max_attempts=5, **kwargs):

    for attempt in range(max_attempts):

        try:
            return get_openai_client().chat.completions.create(**kwargs)

        except Exception as ex:

            if "429" in str(ex):

                wait_time = min((2 ** attempt) * 5, 60)

                time.sleep(wait_time)

            else:
                raise

    raise Exception("Max retry attempts exceeded")


def ask_agent(question, conversation_context=""):

    try:
        table_results = list_log_tables()
    except Exception as error:
        logger.exception("Workspace table discovery failed")
        return "", [], azure_failure_message("inspect the workspace tables", error)

    if not table_results:
        return "", [], "No Log Analytics tables with data were found in this workspace."

    available_tables = sorted({
        str(row.get("__SourceTable", "")).strip()
        for row in table_results
        if row.get("__SourceTable")
    })

    try:
        selection_response = create_completion_with_retry(
            model="gpt-5-mini",
            messages=[
                {
                    "role": "system",
                    "content": """
You are an Azure Log Analytics assistant.
Select every workspace table needed to answer the user's request.
Use only table names from the supplied workspace table list; never invent names.
Return an empty list when no table matches the request.
"""
                },
                {
                    "role": "user",
                    "content": (
                        f"Previous conversation:\n{conversation_context}\n\n"
                        f"Current request:\n{question}"
                        if conversation_context else question
                    )
                },
                {
                    "role": "user",
                    "content": "Workspace tables:\n" + json.dumps(available_tables)
                }
            ],
            tools=selection_tools,
            tool_choice="required"
        )
    except Exception:
        logger.exception("Table-selection model call failed")
        return "", [], "I could not determine the relevant workspace tables. Please try again."

    try:
        selection_message = selection_response.choices[0].message
        selection_call = selection_message.tool_calls[0]
        selected_tables = json.loads(selection_call.function.arguments)["table_names"]
    except (AttributeError, IndexError, KeyError, TypeError, json.JSONDecodeError):
        logger.exception("Table-selection tool response was invalid")
        return "", [], "I could not determine which workspace tables match your request."

    if not isinstance(selected_tables, list):
        return "", [], "I could not determine which workspace tables match your request."

    selected_tables = [
        table for table in selected_tables
        if isinstance(table, str) and table in available_tables
    ]
    if not selected_tables:
        return "", [], "No matching Log Analytics table was found for your request."

    schemas = {}
    schema_errors = []
    max_workers = min(4, len(selected_tables))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(get_table_schema, table_name): table_name
            for table_name in selected_tables
        }
        for future in as_completed(futures):
            table_name = futures[future]
            try:
                schema = future.result()
            except Exception as error:
                logger.exception("Schema lookup failed for table %s", table_name)
                schema_errors.append(error)
                continue
            if schema:
                schemas[table_name] = schema

    if not schemas:
        if schema_errors:
            return "", [], azure_failure_message("retrieve the selected table schemas", schema_errors[0])
        return "", [], "No schema or data was found for the selected workspace tables."

    compacted_schemas = compact_schemas(schemas)
    if not any(compacted_schemas.values()):
        return "", [], "The selected tables returned no usable column definitions."

    try:
        query_response = create_completion_with_retry(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": """
Construct one valid KQL query for the user's request using only the supplied tables and schemas.
Include the requested time filter when appropriate. Use 24h when no duration is specified.
Always call run_kql_query. Do not use any other tables.
"""},
                {"role": "user", "content": question},
                {"role": "user", "content": "Tables and schemas:\n" + json.dumps(compacted_schemas)}
            ],
            tools=query_tools,
            tool_choice="required"
        )
    except Exception:
        logger.exception("KQL-generation model call failed")
        return "", [], "I could not construct a KQL query for that request."

    try:
        assistant_message = query_response.choices[0].message
        tool_call = assistant_message.tool_calls[0]
        arguments = json.loads(
            tool_call.function.arguments.replace("&gt;", ">")
        )
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")
        query = arguments["query"]
        duration = arguments.get("duration", "24h")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if not isinstance(duration, str):
            raise ValueError("duration must be a string")
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.exception("KQL-generation tool response was invalid")
        return "", [], "The generated KQL response was invalid. Please try again."

    try:
        query = validate_generated_kql(query, selected_tables)
    except ValueError as error:
        logger.warning("Generated KQL rejected: %s", error)
        return query, [], f"I could not safely execute the generated KQL: {error}"

    try:
        results = run_kql_query(query, duration=duration)
    except Exception as error:
        logger.exception("Generated KQL execution failed")
        return query, [], azure_failure_message("execute the generated KQL", error)

    answer = build_result_summary(question, query, results)

    return query, results, answer

st.set_page_config(
    page_title="Azure OpenAI KQL Agent",
    layout="wide"
)

st.markdown(
    """
    <style>
    [data-testid="stHeader"] {
        display: none;
    }
    [data-testid="stAppViewContainer"] > .main {
        padding-top: 4.75rem;
    }
    .agent-navbar {
        align-items: center;
        background: #102a43;
        border-bottom: 3px solid #f0b429;
        color: #f7f9fc;
        display: flex;
        height: 3.75rem;
        left: 0;
        padding: 0 2rem;
        position: fixed;
        right: 0;
        top: 0;
        z-index: 1000;
    }
    .agent-navbar-title {
        font-size: 1.05rem;
        font-weight: 700;
        letter-spacing: .01em;
    }
    .agent-navbar-subtitle {
        color: #bcccdc;
        font-size: .82rem;
        margin-left: 1rem;
    }
    @media (max-width: 640px) {
        .agent-navbar {
            padding: 0 1rem;
        }
        .agent-navbar-subtitle {
            display: none;
        }
    }
    </style>
    <nav class="agent-navbar" aria-label="Application navigation">
        <span class="agent-navbar-title">Azure OpenAI Log Analytics Agent</span>
        <span class="agent-navbar-subtitle">KQL workspace assistant</span>
    </nav>
    """,
    unsafe_allow_html=True,
)

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.container(height=620, border=False):
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

            if message["role"] == "assistant" and message.get("query"):
                with st.expander("Generated KQL"):
                    st.code(message["query"], language="sql")

                with st.expander("Raw Results"):
                    if message["results"]:
                        st.dataframe(
                            pd.DataFrame(message["results"]),
                            use_container_width=True
                        )
                    else:
                        st.info("No records returned.")

question = st.chat_input("Ask about your Azure logs...")

if question:
    st.session_state.messages.append({
        "role": "user",
        "content": question,
    })

    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Analyzing Azure logs..."):
            try:
                previous_messages = st.session_state.messages[:-1][-MAX_CONTEXT_MESSAGES:]
                conversation_context = "\n".join(
                    f"{message['role']}: {message['content']}"
                    for message in previous_messages
                )
                query, results, answer = ask_agent(
                    question,
                    conversation_context=conversation_context,
                )
            except Exception:
                logger.exception("Unhandled agent request failure")
                query, results = "", []
                answer = "I could not complete that request. Check the application logs for details."

        st.markdown(answer)

        if query:
            with st.expander("Generated KQL"):
                st.code(query, language="sql")

            with st.expander("Raw Results"):
                if results:
                    st.dataframe(
                        pd.DataFrame(results),
                        use_container_width=True
                    )
                else:
                    st.info("No records returned.")

    st.session_state.messages.append({
        "role": "assistant",
        "content": answer,
        "query": query,
        "results": results,
    })