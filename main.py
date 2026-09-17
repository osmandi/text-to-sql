from langchain.chat_models import init_chat_model
import pathlib
import requests
import sqlite3
from langchain.tools import tool

from typing import Literal
from langchain.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode


@tool
def sql_db_list_tables() -> str:
    """Input is an empty string, output is a comma-separated list of tables in the database."""
    con = sqlite3.connect("Chinook.db")

    try:
        cursor = con.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall() if not row[0].startswith("sqlite_")]

    finally:
        con.close()

    return ", ".join(tables)

@tool
def sql_db_schema(table_names: str) -> str:
    """
    Input to this tool is a comma-separated list of tables, output is the schema and sample rows of those tables.
    Be sure thatthet tables actually exist by calling sql_db_list_tables() first!
    Example input: table1, table2, table3
    """
    con = sqlite3.connect("Chinook.db")
    try:
        cursor = con.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        valid_tables = {
            row[0] for row in cursor.fetchall() if not row[0].startswith("sqlite_")
        }
        results = []
        for table in table_names.split(","):
            table = table.strip()
            if table not in valid_tables:
                results.append(
                    f"Error: table_names {{{table!r}}} not found in database"
                )
                continue
            cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?;",
                (table,),
            )
            schema_row = cursor.fetchone()
            if schema_row:
                results.append(schema_row[0])
                try:
                    quoted_table = '"' + table.replace('"', '""') + '"'
                    cursor.execute(f"SELECT * FROM {quoted_table} LIMIT 3;")
                    rows = cursor.fetchall()
                    if rows:
                        col_names = [description[0] for description in cursor.description]
                        results.append(
                            f"/*\n3 rows from {table} table:\n"
                            + "\t".join(col_names)
                            + "\n"
                            + "\n".join(
                                "\t".join(str(x) for x in row) for row in rows
                            )
                            + "\n*/"
                        )
                except Exception as e:
                    results.append(f"Error fetching sample rows: {e}")
    finally:
        con.close()
    return "\n\n".join(results)


@tool
def sql_db_query(query: str) -> str:
    """
    Input to this tool is a detailled and correct SQL query, output is a result from the database.
    If the query is not correct, an error message will be returned.
    IF an error is returned, rewrite the query, check the query, and try again.
    IF you encounter an issue with Unknown column 'xxx' in 'filend list', use sql_db_schema to query thte correct table fields.
    """

    con = sqlite3.connect("Chinook.db")
    try:
        cursor = con.cursor()
        cursor.execute(query)
        result = cursor.fetchall()
    except Exception as e:
        return f"Error: {e}"
    finally:
        con.close()

    return str(result)



if __name__ == "__main__":

    # Configure the model
    model = init_chat_model("gpt-5.5")

    # Configure the database

    url = "https://storage.googleapis.com/benchmarks-artifacts/chinook/Chinook.db"
    local_path = pathlib.Path("Chinook.db")

    if local_path.exists():
        print(f"{local_path} already exists, skipping download.")
    else:
        response = requests.get(url, timeout=60)
        if response.status_code == 200:
            local_path.write_bytes(response.content)
            print(f"File downloaded and saved as {local_path}")
        else:
            print(f"Failed to download the file. Status code: {response.status_code}")


    tools = [sql_db_list_tables, sql_db_schema, sql_db_query]

    # Print tools and its description
    for t in tools:
        print(f"{t.name}: {t.description}\n")

    get_schema_tool = next(tool for tool in tools if tool.name == "sql_db_schema")
    get_schema_node = ToolNode([get_schema_tool], name="get_schema")
    run_query_tool = next(tool for tool in tools if tool.name == "sql_db_query")
    run_query_node = ToolNode([run_query_tool], name="run_query")

    # Example: create a predetermined tool call
    def list_tables(state: MessagesState):
        tool_call = {
            "name": "sql_db_list_tables",
            "args": {},
            "id": "abc123",
            "type": "tool_call",
        }
        tool_call_message = AIMessage(content="", tool_calls=[tool_call])

        list_tables_tool = next(tool for tool in tools if tool.name == "sql_db_list_tables")
        tool_message = list_tables_tool.invoke(tool_call)
        response = AIMessage(f"Available tables: {tool_message.content}")

        return {"messages": [tool_call_message, tool_message, response]}

    # Example: force a model to create a tool call
    def call_get_schema(state: MessagesState):
        # Note that LangChain enforces that all models accept `tool_choice="any"`
        # as well as `tool_choice=<string name of tool>`.
        llm_with_tools = model.bind_tools([get_schema_tool], tool_choice="any")
        response = llm_with_tools.invoke(state["messages"])

        return {"messages": [response]}

    generate_query_system_prompt = """
    You are an agent designed to interact with a SQL database.
    Given an input question, create a syntactically correct {dialect} query to run,
    then look at the results of the query and return the answer. Unless the user
    specifies a specific number of examples they wish to obtain, always limit your
    query to at most {top_k} results.
    
    You can order the results by a relevant column to return the most interesting
    examples in the database. Never query for all the columns from a specific table,
    only ask for the relevant columns given the question.
    
    DO NOT make any DML statements (INSERT, UPDATE, DELETE, DROP etc.) to the database.
    """.format(
        dialect="sqlite",
        top_k=5,
    )

    def generate_query(state: MessagesState):
        system_message = {
            "role": "system",
            "content": generate_query_system_prompt,
        }
        # We do not force a tool call here, to allow the model to
        # respond naturally when it obtains the solution.
        llm_with_tools = model.bind_tools([run_query_tool])
        response = llm_with_tools.invoke([system_message] + state["messages"])

        return {"messages": [response]}

    check_query_system_prompt = """
    You are a SQL expert with a strong attention to detail.
    Double check the {dialect} query for common mistakes, including:
    - Using NOT IN with NULL values
    - Using UNION when UNION ALL should have been used
    - Using BETWEEN for exclusive ranges
    - Data type mismatch in predicates
    - Properly quoting identifiers
    - Using the correct number of arguments for functions
    - Casting to the correct data type
    - Using the proper columns for joins
    
    If there are any of the above mistakes, rewrite the query. If there are no mistakes,
    just reproduce the original query.
    
    You will call the appropriate tool to execute the query after running this check.
    """.format(dialect="sqlite")

    def check_query(state: MessagesState):
        system_message = {
            "role": "system",
            "content": check_query_system_prompt,
        }

        # Generate an artificial user message to check
        tool_call = state["messages"][-1].tool_calls[0]
        user_message = {"role": "user", "content": tool_call["args"]["query"]}
        llm_with_tools = model.bind_tools([run_query_tool], tool_choice="any")
        response = llm_with_tools.invoke([system_message, user_message])
        response.id = state["messages"][-1].id

        return {"messages": [response]}


    def should_continue(state: MessagesState) -> Literal[END, "check_query"]:
        messages = state["messages"]
        last_message = messages[-1]
        if not last_message.tool_calls:
            return END
        else:
            return "check_query"


    builder = StateGraph(MessagesState)
    builder.add_node(list_tables)
    builder.add_node(call_get_schema)
    builder.add_node(get_schema_node, "get_schema")
    builder.add_node(generate_query)
    builder.add_node(check_query)
    builder.add_node(run_query_node, "run_query")

    builder.add_edge(START, "list_tables")
    builder.add_edge("list_tables", "call_get_schema")
    builder.add_edge("call_get_schema", "get_schema")
    builder.add_edge("get_schema", "generate_query")
    builder.add_conditional_edges(
        "generate_query",
        should_continue,
    )
    builder.add_edge("check_query", "run_query")
    builder.add_edge("run_query", "generate_query")

    agent = builder.compile()

    # Visualize the application
    import pathlib
    pathlib.Path("graph.png").write_bytes(agent.get_graph().draw_mermaid_png())

    # Invoke the graph
    #question = "Which genre on average has the longest tracks?"
    question = "How many genres there are?"

    stream = agent.stream_events(
        {"messages": [{"role": "user", "content": question}]},
        version="v3",
    )
    for message in stream.messages:
        for token in message.text:
            print(token, end="", flush=True)

    final_state = stream.output
    print(final_state)
    print(dir(final_state))
    for message in final_state["messages"]:
        print(message.text)

    print("Final node")
    print(final_state["messages"][-1].text)