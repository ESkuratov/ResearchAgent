# https://github.com/jkmaina/LangGraphProjects/blob/main/chapter13/lesson13a.py
# This is a Research Assistant application that uses LangGraph to coordinate multiple agents
# for conducting research, and producing PDF reports.
import os
import logging
import functools
import operator
from fpdf import FPDF
from dotenv import load_dotenv
from typing import Sequence, Annotated, Literal
from typing_extensions import TypedDict

from pydantic import BaseModel

from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch
from langchain_experimental.tools import PythonREPLTool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, BaseMessage
from langchain_core.tools import tool

from langgraph.graph import END, StateGraph, START

from langgraph_supervisor import create_supervisor
from langgraph.prebuilt import create_react_agent

load_dotenv()
api_key = os.getenv("VSEGPT_API_KEY")
base_url = os.getenv("base_url")
tavily_key = os.getenv("TAVILY_API_KEY")

if not api_key:
    raise ValueError("VSEGPT_API_KEY not found in environment variables")

if not os.environ.get("TAVILY_API_KEY"):
    os.environ["TAVILY_API_KEY"] = getpass.getpass("Tavily API key:\n")

logging.info("Загрузка OpenAI API ключа")


tavily_tool = TavilySearch(
    max_results=10,
    )


def agent_node(state, agent, name):
    result = agent.invoke(state)
    return {
        "messages": [HumanMessage(content=result["messages"][-1].content, name=name)]
    }

members = ["Researcher", "Coder"]

# Add a PDF generation tool
@tool
def generate_pdf(content: str, title: str = "Research Report") -> str:
    """
    Generate a PDF document with the given content and title.
    
    Args:
        content: The text content to include in the PDF
        title: The title of the PDF document
    
    Returns:
        A string confirming the PDF was created and its location
    """
    try:
        pdf = FPDF()
        pdf.add_page()
        
        # Set font for title
        pdf.set_font("Arial", "B", 16)
        pdf.cell(0, 10, title, 0, 1, 'C')
        pdf.ln(10)
        
        # Set font for content
        pdf.set_font("Arial", "", 12)
        
        # Split content into lines and add to PDF
        lines = content.split('\n')
        for line in lines:
            # Process each paragraph
            if line.strip():
                pdf.multi_cell(0, 10, line)
                pdf.ln(5)
        
        # Save the PDF
        filename = f"{title.replace(' ', '_')}.pdf"
        pdf.output(filename)
        return f"PDF successfully created: {filename}"
    except Exception as e:
        return f"Error creating PDF: {str(e)}"

system_prompt = (
    "You are a supervisor tasked with managing a conversation between the"
    " following workers:  {members}. Given the following user request,"
    " respond with the worker to act next. Each worker will perform a"
    " task and respond with their results and status. When finished,"
    " respond with FINISH."
)
# Our team supervisor is an LLM node. It just picks the next agent to process
# and decides when the work is completed
options = ["FINISH"] + members


class routeResponse(BaseModel):
    next: Literal[*options]


prompt = ChatPromptTemplate.from_messages(
    [
        ("system", system_prompt),
        MessagesPlaceholder(variable_name="messages"),
        (
            "system",
            "Given the conversation above, who should act next?"
            " Or should we FINISH? Select one of: {options}",
        ),
    ]
).partial(options=str(options), members=", ".join(members))

# Initialize the model
llm = ChatOpenAI(
    model="openai/gpt-4.1-mini",
    api_key = api_key,
    base_url = base_url)


def supervisor_agent(state):
    supervisor_chain = prompt | llm.with_structured_output(routeResponse)
    return supervisor_chain.invoke(state)


# The agent state is the input to each node in the graph
class AgentState(TypedDict):
    # The annotation tells the graph that new messages will always
    # be added to the current states
    messages: Annotated[Sequence[BaseMessage], operator.add]
    # The 'next' field indicates where to route to next
    next: str


research_agent = create_react_agent(llm, tools=[tavily_tool])
research_node = functools.partial(agent_node, agent=research_agent, name="Researcher")

# NOTE: THIS PERFORMS ARBITRARY CODE EXECUTION. PROCEED WITH CAUTION
generator_pd_agent = create_react_agent(llm, tools=[generate_pdf])
generator_pdf_node = functools.partial(agent_node, agent=generator_pd_agent, name="Generator_pdf")

workflow = StateGraph(AgentState)
workflow.add_node("Researcher", research_node)
workflow.add_node("Generator_pdf", generator_pdf_node)
workflow.add_node("supervisor", supervisor_agent)


for member in members:
    # We want our workers to ALWAYS "report back" to the supervisor when done
    workflow.add_edge(member, "supervisor")


# The supervisor populates the "next" field in the graph state
# which routes to a node or finishes
conditional_map = {k: k for k in members}
conditional_map["FINISH"] = END
workflow.add_conditional_edges("supervisor", lambda x: x["next"], conditional_map)
# Finally, add entrypoint
workflow.add_edge(START, "supervisor")

graph = workflow.compile()

for s in graph.stream(
    {
        "messages": [
            HumanMessage(content="Write a brief pdf research report recent advances in AI and summarize.")
        ]
    }
):
    if "__end__" not in s:
        print(s)
        print("----")
