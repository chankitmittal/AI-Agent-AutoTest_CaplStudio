import os
import json
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Literal, Optional, Any
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage
import re
import sys
import time

load_dotenv()
OUTPUT_DIR = "./Test_Specifications"
PROMPT_DIR = "./SystemPrompt"
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
Path(PROMPT_DIR).mkdir(parents=True, exist_ok=True)
LOCAL_LLM_MODEL = "qwen3:8b"
ONLINE_LLM_MODEL = "gemini-3.1-flash-lite"

logger = logging.getLogger(__name__)


def load_system_prompt(domain_name: str) -> str:
    """
    Loads the system prompt text for a specific domain from a markdown file.

    Args:
        domain_name (str): The name of the domain (e.g., 'bootloader') used to locate the prompt file.

    Returns:
        str: The content of the domain-specific prompt.

    Raises:
        FileNotFoundError: If the corresponding prompt file is not found.
    """
    prompt_path = Path(PROMPT_DIR) / f"{domain_name}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}. Please create it.")
    return prompt_path.read_text(encoding="utf-8")


class TestStep(BaseModel):
    """
    Represents a single step within a test case.
    """
    action: str = Field(description="The specific action, stimulus, or transmitted UDS request.")
    expected_response: str = Field(description="The exact expected ECU response for this specific action.")


class TestCase(BaseModel):
    """
    Represents a complete test case with preconditions, steps, and postconditions.
    """
    title: str = Field(description="Clear, concise test case title including the intent and target component.")
    type: Literal["Positive", "Negative"] = Field(description="Strictly classify the test execution path.")
    preconditions: List[str] = Field(description="Static environment setups or initial values required BEFORE execution.")
    steps: List[TestStep] = Field(description="Sequential list of steps, coupling every action strictly to its expected response.")
    postconditions: List[str] = Field(description="The final state of the system or environment under test after execution concludes.")


class TestSpecification(BaseModel):
    """
    Represents a full test specification document, including a scratchpad and generated test cases.
    """
    sequence_verification: str = Field(description="MANDATORY THOUGHT PROCESS SCRATCHPAD: Draft the logical sequence and ensure domain rules are met BEFORE writing the structural JSON.")
    test_cases: List[TestCase] = Field(description="Collection of generated test cases.", max_length=5)


def generate_test_specification(requirement_text: str, output_filename: str, chosen_domain: str, llm: Any) -> Optional[TestSpecification]:
    """
    Generates a formal test specification based on a software requirement.

    Args:
        requirement_text (str): The natural language software requirement.
        output_filename (str): Base name for any output contexts (unused in logic but passed by GUI).
        chosen_domain (str): The specific automotive domain rules to apply.
        llm (Any): The LangChain chat model instance to use for generation.

    Returns:
        Optional[TestSpecification]: The generated structured test specification, or None if generation fails.
    """
    logger.info("Loading system rules for domain: '%s'...", chosen_domain)
    try:
        system_instruction = load_system_prompt(chosen_domain)
    except FileNotFoundError as e:
        logger.error("Error: %s", e)
        return None

    prompt_template = ChatPromptTemplate.from_messages([
        SystemMessage(content=system_instruction),
        ("human", "REQUIREMENT TO ANALYZE:\n{requirement}")
    ])
    structured_llm = llm.with_structured_output(TestSpecification)
    generation_chain = prompt_template | structured_llm

    logger.info("Analyzing sequence requirements and generating JSON specification...")
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("-> Generation Attempt %d/%d...", attempt, max_retries)
            result = generation_chain.invoke({"requirement": requirement_text})
            logger.info("Generation Complete!")
            logger.debug("--- Model's Sequence Verification Check ---\n%s", result.sequence_verification)
            return result
        except Exception as e:
            # Catch Pydantic validation errors or JSON parsing failures
            logger.warning("Attempt %d failed: Model produced malformed structure.", attempt)
            if attempt < max_retries:
                logger.info("Retrying in 5 seconds...")
                time.sleep(5)
            else:
                logger.error("Critical Error: Model failed to generate valid JSON after maximum retries.")
                logger.error("Tip: The sequence logic may be too complex for the current model, or the context window limit was reached.")
    return None
