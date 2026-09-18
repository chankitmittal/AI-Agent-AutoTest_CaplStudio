import os
import pickle
import re
import hashlib
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_ollama import OllamaEmbeddings, ChatOllama
from langchain_chroma import Chroma
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers.ensemble import EnsembleRetriever
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv
import time
import tempfile
import subprocess
import uuid
import concurrent.futures
from typing import TypedDict, Annotated, List, Any, Tuple, Optional, Dict
from operator import add
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langgraph.graph import StateGraph, END

logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# Configuration Setup
# ---------------------------------------------------------
load_dotenv()
DOCS_DIR = "./CAPL"
EMBEDDING_MODEL = "nomic-embed-text"
LLM_LOCAL_MODEL = "qwen2.5-coder:7b"
# LLM_ONLINE_MODEL = "gemma-4-26b-a4b-it"
LLM_ONLINE_MODEL = "gemma-4-31b-it"
SHARED_DB_DIR = "./chroma_db"
BM25_FILE = "./bm25_chunks.pkl"
MEMORY_DB_DIR = "./memory_chroma_db"
MEMORY_BM25_FILE = "./memory_bm25_chunks.pkl"


# Define the state that gets passed between nodes
class CodeGenState(TypedDict):
    user_query: str
    context: str
    memory_context: str
    dependency_cheat_sheet: str
    global_variables: str
    allowed_functions: List[str]
    # Conversational History (append-only via `add`)
    messages: Annotated[list[BaseMessage], add]
    attempt: int
    max_retries: int
    # State component for the LLM!
    llm: Any
    # Outputs
    result: Any  # Will hold the CAPLGeneration Pydantic object
    final_code: str
    funcs_called: List[str]
    is_valid: bool
    fallback_triggered: bool
    fallback_code: str


class MemorySummary(BaseModel):
    """Pydantic schema for summarizing approved test cases."""
    intent: str = Field(description="A 1-2 sentence technical explanation of what this test case verifies.")
    test_category: str = Field(description="A single category word (e.g., Diagnostics, NetworkManagement, FaultInjection).")


class CAPLGeneration(BaseModel):
    """Pydantic schema enforcing structured JSON output from the LLM."""
    is_supported: bool = Field(description="True if the provided snippets contain the necessary wrappers (e.g., SOME/IP, UDS) to fulfill the core request. False if the requested functionality is missing from the snippets.")
    unsupported_reason: str = Field(description="If is_supported is False, briefly explain what protocol or wrapper is missing from the database. If True, leave empty.")
    dependencies: List[str] = Field(description="List of exact .cin files required based on the dependency cheat sheet.")
    functions_called: List[str] = Field(description="A list of all function names called inside the test logic.")
    test_case_name: str = Field(description="The function name for the test case.")
    capl_code: str = Field(description="The final, compilable CAPL script. Output ONLY raw C-code. Do NOT include markdown blocks, and absolutely NO explanatory comments (//) inside the code.")


class QueryVariations(BaseModel):
    """Schema for generating multiple search perspectives."""
    hex_protocol_query: str = Field(
        default="",
        description="Extract ONLY the core protocol and main hex IDs (e.g., 'UDS 0x10 0x22'). Keep it under 10 words."
    )
    concept_query: str = Field(
        default="",
        description="The query rewritten to focus on the abstract testing concept (e.g., 'Verify ECU state transition')."
    )
    expanded_query: str = Field(
        default="",
        description="The original query enriched with relevant Vector CAPL domain keywords."
    )


def node_generate_code(state: CodeGenState) -> dict:
    """
    Generates CAPL code using the LLM based on retrieved context.

    Args:
        state (CodeGenState): The current state of the workflow graph.

    Returns:
        dict: A dictionary containing the updated result and attempt count.
    """
    attempt = state.get("attempt", 0) + 1
    logger.info("[Graph] Generating Code (Attempt %d/%d)...", attempt, state['max_retries'])
    
    llm = state["llm"]
    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are an expert Vector CANoe test engineer. 
        Your task is to generate complete, compilable CAPL code based ONLY on the provided CODE SNIPPETS.
        Populate the required output schema strictly according to the following rules:

        --- 0. THE KNOWLEDGE BOUNDARY (CRITICAL) ---
        Evaluate the user's request against the provided CODE SNIPPETS.
        If the user asks for a protocol or domain and there are NO relevant wrappers for that protocol in the snippets:
        1. Set `is_supported` to false.
        2. Set `unsupported_reason` to explain what is missing.

        --- 1. MANDATORY CODE SKELETON ---
        /* =========================================================================
           TEST CASE: [Insert Test Name]
           ========================================================================= */
        includes
        {{
        }}

        testcase [test_case_name]()
        {{
        }}

        --- 2. STRICT ABSTRACTION RULE ---
        Do NOT write raw CANoe mechanics. You must ONLY call the wrappers provided in the CODE SNIPPETS.

        --- 3. DEPENDENCIES & INCLUDES ---
        - Review the DEPENDENCY CHEAT SHEET.
        - If you call a function listed there, you MUST place its required .cin file inside the `includes {{ }}` block above. 

        --- 4. TIMING & DELAYS (CRITICAL) ---
        You MUST ONLY use `testWaitForTimeout(<milliseconds>);`.
        NEVER use `sleep()`, `delay()`, `setTimer()`, or `on timer` events.

        --- 5. VARIABLES ---
        DO NOT generate a `variables {{ ... }}` block. Assume all global objects are handled.

        --- 6. VERIFIED PAST EXAMPLES (CRITICAL) ---
        If there are test cases provided below, they are historically verified and guaranteed to work.
        You MUST PRIORITIZE THEIR STRUCTURE AND LOGIC over generating from scratch. 
        If the user's request matches the intent of a past example, use it as your primary template.
        <verified_examples>
        {memory_context}
        </verified_examples>

        DEPENDENCY CHEAT SHEET:
        {dependency_cheat_sheet}

        GLOBAL VARIABLE DICTIONARY:
        {global_variables}

        CODE SNIPPETS:
        {context}"""),

        ("human", "USER IMPLEMENTATION REQUEST: {query}"),
        *state["messages"]
    ])
    structured_llm = llm.with_structured_output(CAPLGeneration, method="json_mode")
    chain = prompt | structured_llm

    max_api_retries = 3
    api_attempt = 0
    result = None
    
    while api_attempt < max_api_retries:
        try:
            result = chain.invoke({
                "context": state["context"],
                "query": state["user_query"],
                "dependency_cheat_sheet": state["dependency_cheat_sheet"],
                "global_variables": state["global_variables"],
                "memory_context": state["memory_context"]
            })
            break
        except Exception as e:
            api_attempt += 1
            print(f"[Graph] API Call Exception (Attempt {api_attempt}/{max_api_retries}): {e}")
            if api_attempt < max_api_retries:
                import time
                print("Waiting 5 seconds before retrying API call...")
                time.sleep(5)
            else:
                # Force a hallucination so the validator rejects it and triggers a retry
                result = CAPLGeneration(
                    is_supported=True,
                    unsupported_reason="",
                    dependencies=[],
                    functions_called=["LLM_JSON_PARSING_FAILED"],
                    test_case_name="error",
                    capl_code="/* Error: LLM output invalid JSON or API failed completely */"
                )
    return {"result": result, "attempt": attempt}


def node_validate_code(state: CodeGenState) -> dict:
    """
    Validates the generated CAPL code against business rules and syntax checks.

    Args:
        state (CodeGenState): The current state of the workflow graph containing the generated code.

    Returns:
        dict: A dictionary containing validation status, potential fallback code, or feedback messages.
    """
    result: CAPLGeneration = state["result"]
    logger.info("[Graph] Validating LLM Output...")
    logger.debug("--- RAW LLM OUTPUT ---\n%s\n----------------------", result.capl_code)
    
    if not result.is_supported:
        logger.warning("[Graph] WRAPPERS NOT FOUND: Switching to Native CAPL Fallback")
        logger.warning("Reason: %s", result.unsupported_reason)
        llm = state["llm"]
        fallback_prompt = ChatPromptTemplate.from_messages([
            ("system",
             "You are an expert Vector CANoe test engineer. Write standard, native CAPL code to fulfill the user's request as best as possible. You MAY use raw CANoe mechanics."),
            ("human", "USER IMPLEMENTATION REQUEST: {query}")
        ])
        fallback_code = (fallback_prompt | llm | StrOutputParser()).invoke({"query": state["user_query"]})
        warning_header = (
            "/* =========================================================================\n"
            "   ⚠️ AUTOMATED GENERATION WARNING\n"
            "   =========================================================================\n"
            f"   Custom wrappers for this request were NOT found in the database.\n"
            f"   LLM Reason: {result.unsupported_reason}\n\n"
            "   This script was generated using NATIVE CAPL as a best-effort fallback.\n"
            "   ========================================================================= */\n\n"
        )
        final_fallback_code = warning_header + fallback_code.strip()
        final_fallback_code = re.sub(r'^```capl\n|^```c\n|^```\n', '', final_fallback_code, flags=re.MULTILINE)
        final_fallback_code = re.sub(r'```$', '', final_fallback_code, flags=re.MULTILINE)
        return {"fallback_triggered": True, "fallback_code": final_fallback_code, "is_valid": False}

    hallucinations = []
    safe_allowed_functions = [fn.lower().strip() for fn in state["allowed_functions"]]
    for call in result.functions_called:
        if call.lower().strip() not in safe_allowed_functions:
            hallucinations.append(call)
            
    forbidden_timers = ["sleep", "delay", "setTimer", "wait"]
    for forbidden in forbidden_timers:
        if re.search(r'\b' + forbidden + r'\s*\(', result.capl_code, re.IGNORECASE):
            hallucinations.append(f"{forbidden}() -> ILLEGAL TIMER. You must use testWaitForTimeout() instead.")
            
    if result.capl_code.count('(') != result.capl_code.count(')'):
        hallucinations.append("UNBALANCED_PARENTHESES -> Syntax error detected.")
        
    if re.search(r'[`~@$]', result.capl_code):
        hallucinations.append("ILLEGAL_CHARACTERS -> Code contains garbage syntax.")
        
    if '|/' in result.capl_code:
        hallucinations.append("STUTTER_ARTIFACT -> Found '|/' in code.")
        
    if len(hallucinations) == 0:
        logger.info("[Graph] Validation Passed. No hallucinations detected.")
        final_code = re.sub(r'includes\s*\{[^}]*\}', '', result.capl_code, flags=re.IGNORECASE)
        final_code = re.sub(r'#include\s*["<][^">]+[">]', '', final_code, flags=re.IGNORECASE)
        final_code = re.sub(r'\n{3,}', '\n\n', final_code)
        
        safe_deps = set(result.dependencies) if result.dependencies else set()
        safe_deps.add("Global_Variables.cin")
        
        include_lines = "\n".join([f'  #include "{dep}"' for dep in sorted(safe_deps)])
        include_block = f"includes\n{{\n{include_lines}\n}}\n"
        final_code = include_block + "\n" + final_code.strip()
        logger.debug(final_code)
        return {"is_valid": True, "final_code": final_code, "funcs_called": result.functions_called}
    else:
        logger.warning("[Graph] Validation Failed. Issues detected: %s", hallucinations)
        if "LLM_JSON_PARSING_FAILED" in hallucinations:
            sys_rejection = HumanMessage(content=(
                "CRITICAL ERROR: Your previous response was not a valid JSON object or was completely unparseable. "
                "You MUST output strictly valid JSON matching the exact schema. Do not write markdown text or raw code outside the JSON."
            ))
            return {"is_valid": False, "messages": [sys_rejection]}
        else:
            ai_msg = AIMessage(content=f"```json\n{result.model_dump_json(indent=2)}\n```")
            sys_rejection = HumanMessage(content=(
                f"CRITICAL ERROR: Your code generated the following issues: {', '.join(set(hallucinations))}. "
                "Review the snippet definitions, fix syntax errors, use ONLY provided wrappers, and rewrite the code."
            ))
            return {"is_valid": False, "messages": [ai_msg, sys_rejection]}


def route_next_step(state: CodeGenState) -> str:
    """
    Determines the next node in the graph execution.

    Args:
        state (CodeGenState): The current state.

    Returns:
        str: The name of the next node or END.
    """
    if state.get("fallback_triggered"):
        return END
    if state.get("is_valid"):
        return END
    if state["attempt"] >= state["max_retries"]:
        return END
    return "generate"


def expand_query_multi_way(llm: Any, user_query: str) -> List[str]:
    """
    Generates distinct query variations for parallel vector search.

    Args:
        llm (Any): The LLM to use for query expansion.
        user_query (str): The original user query.

    Returns:
        List[str]: A list of query variations including the original query.
    """
    logger.info("Generating contextual query variations...")
    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a Technical Query Expander for an automotive CANoe RAG system.
            Break the user's request into three distinct search queries.
            CRITICAL: If the user provides a massive sequence of steps, DO NOT copy the whole sequence. 
            Extract only the top 3 most important concepts and hex codes for the search queries."""),
        ("human", "{query}")
    ])
    structured_llm = llm.with_structured_output(QueryVariations, method="json_mode")
    chain = prompt | structured_llm
    try:
        variations = chain.invoke({"query": user_query})
        queries = [
            user_query,
            variations.hex_protocol_query,
            variations.concept_query,
            variations.expanded_query
        ]
        logger.info("-> Variations generated: %d", len(queries))
        return queries
    except Exception as e:
        logger.error("Query expansion failed (%s). Falling back to original query.", e)
        return [user_query]


def retrieve_hybrid_parallel(ensemble_retriever: Any, queries: List[str]) -> List[Document]:
    """
    Runs multiple queries in parallel and de-duplicates the results.

    Args:
        ensemble_retriever (Any): The retriever to use for fetching documents.
        queries (List[str]): The list of queries to search.

    Returns:
        List[Document]: A list of unique retrieved documents.
    """
    unique_docs: Dict[str, Document] = {}

    def fetch(q: str) -> List[Document]:
        return ensemble_retriever.invoke(q)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(queries)) as executor:
        results = executor.map(fetch, queries)

    for doc_list in results:
        for doc in doc_list:
            if doc.page_content not in unique_docs:
                unique_docs[doc.page_content] = doc
    return list(unique_docs.values())


def extract_capl_intelligence(filepath: Path) -> dict:
    """
    Parses a .can or .cin file to extract its dependencies and provided functions.

    Args:
        filepath (Path): The path to the file.

    Returns:
        dict: Extracted metadata including file name, dependencies, and provided functions.
    """
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except FileNotFoundError:
        return {"file_name": filepath.name, "dependencies": [], "provided_functions": []}
        
    dependencies = []
    includes_pattern = re.compile(r'["<]?([a-zA-Z0-9_.-]+\.cin)[">]?', re.IGNORECASE)
    includes_block_match = re.search(r'includes\s*\{([^}]*)\}', content, re.IGNORECASE)

    if includes_block_match:
        dependencies = includes_pattern.findall(includes_block_match.group(1))
        
    function_pattern = re.compile(r'\n(?:void|int|dword|char|byte|long|testcase)\s+([a-zA-Z0-9_]+)\s*\(', re.IGNORECASE)
    provided_functions = function_pattern.findall(content)
    
    return {
        "file_name": filepath.name,
        "dependencies": dependencies,
        "provided_functions": provided_functions
    }


def split_capl_logical_blocks(raw_text: str) -> List[str]:
    """
    Splits CAPL code into syntactically complete blocks using regex lookaheads.

    Args:
        raw_text (str): The raw code text.

    Returns:
        List[str]: A list of code blocks.
    """
    pattern = re.compile(
        r'\n(?='
        r'includes\s*\{|'
        r'variables\s*\{|'
        r'testcase\s+[a-zA-Z0-9_]+\s*\(|'
        r'(?:void|int|dword|char|byte|long|float|double)\s+[a-zA-Z0-9_]+\s*\(|'
        r'on\s+(?:message|timer|key|envVar|sysVar|start|preStart|stopMeasurement|diagRequest|diagResponse)'
        r')',
        re.IGNORECASE
    )
    raw_chunks = pattern.split(raw_text)
    return [chunk.strip() for chunk in raw_chunks if chunk.strip()]


def extract_global_variables(filepath: Path) -> str:
    """
    Safely extracts the variables {...} block by matching brace counts.

    Args:
        filepath (Path): The path to the CAPL file.

    Returns:
        str: The extracted variables block, or an empty string if not found.
    """
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except FileNotFoundError:
        return ""
        
    match = re.search(r'\bvariables\s*\{', content, re.IGNORECASE)
    if not match:
        return ""
        
    start_index = match.end() - 1
    brace_count = 0
    for i in range(start_index, len(content)):
        if content[i] == '{':
            brace_count += 1
        elif content[i] == '}':
            brace_count -= 1
        if brace_count == 0:
            return content[start_index:i + 1]
    return ""


def get_file_hash(filepath: Path) -> str:
    """
    Generates an MD5 hash of file content to prevent duplicate ingestion.

    Args:
        filepath (Path): The path to the file.

    Returns:
        str: The computed MD5 hash.
    """
    with open(filepath, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


def edit_code_in_editor(initial_code: str) -> str:
    """
    Opens the generated CAPL code in the system's default text editor for manual correction.

    Args:
        initial_code (str): The initial code to open in the editor.

    Returns:
        str: The code after the user has closed the editor.
    """
    editor = os.environ.get('EDITOR', 'notepad' if os.name == 'nt' else 'nano')
    with tempfile.NamedTemporaryFile(suffix=".can", mode='w+', delete=False) as tf:
        tf.write(initial_code)
        tf.flush()
        temp_filename = tf.name
        
    try:
        logger.info("Opening code in %s... (Close the editor when finished saving)", editor)
        subprocess.call([editor, temp_filename])
        with open(temp_filename, 'r', encoding='utf-8') as tf:
            edited_code = tf.read()
    finally:
        os.remove(temp_filename)
        
    return edited_code


# ---------------------------------------------------------
# Ingestion & Vector DB
# ---------------------------------------------------------
def ingest_capl_codebase(directory_path: str, embeddings: Any, db_dir: str) -> Tuple[Optional[Any], Optional[List[Document]]]:
    """
    Processes directories of CAPL files, chunking and embedding them into ChromaDB.

    Args:
        directory_path (str): The path to the source directory.
        embeddings (Any): The embedding model instance.
        db_dir (str): The path to store the ChromaDB database.

    Returns:
        Tuple[Optional[Any], Optional[List[Document]]]: The initialized vector store and chunks, or (None, None) if missing.
    """
    all_local_chunks = []
    seen_hashes = set()
    base_dir = Path(directory_path)
    logger.info("Scanning '%s' for files...", directory_path)
    
    if not base_dir.exists():
        logger.error("Error: Directory '%s' not found.", directory_path)
        return None, None
        
    for filepath in base_dir.rglob("*.[c|C][a|A|i|I][n|N]"):
        try:
            file_hash = get_file_hash(filepath)
            if file_hash in seen_hashes:
                logger.info("Skipping duplicate shared library: %s", filepath.name)
                continue
            seen_hashes.add(file_hash)
        except Exception as e:
            logger.warning("Failed to hash %s: %s", filepath.name, e)
            continue

        logger.info("Processing and mapping %s...", filepath.name)
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                raw_code = f.read()
        except Exception as e:
            logger.error("Failed to read %s: %s", filepath.name, e)
            continue

        file_intel = extract_capl_intelligence(filepath)
        string_chunks = split_capl_logical_blocks(raw_code)

        for text_chunk in string_chunks:
            if len(text_chunk) < 20:
                continue
            doc = Document(page_content=text_chunk)
            doc.metadata['source'] = file_intel['file_name']
            doc.metadata['dependencies'] = ", ".join(file_intel['dependencies'])
            doc.metadata['exports'] = ", ".join(file_intel['provided_functions'])
            all_local_chunks.append(doc)

        logger.info("Processed %s -> Generated %d logical chunks.", filepath.name, len(string_chunks))

    if not all_local_chunks:
        logger.warning("No valid CAPL files found.")
        return None, None
        
    logger.info("\nTotal chunks generated: %d\nBuilding ChromaDB...", len(all_local_chunks))
    vectorstore = Chroma(embedding_function=embeddings, persist_directory=db_dir)
    batch_size = 5
    for i in range(0, len(all_local_chunks), batch_size):
        batch = all_local_chunks[i: i + batch_size]
        try:
            vectorstore.add_documents(batch)
        except Exception:
            for chunk in batch:
                try:
                    vectorstore.add_documents([chunk])
                except Exception:
                    continue
                    
    logger.info("Building BM25 Index...")
    with open(BM25_FILE, 'wb') as f:
        pickle.dump(all_local_chunks, f)
    return vectorstore, all_local_chunks


def save_approved_code_to_memory(llm: Any, embeddings: Any, user_query: str, approved_code: str, functions_called: List[str]) -> None:
    """
    Summarizes and saves user-approved CAPL code to the Memory Databases.

    Args:
        llm (Any): The LLM to use for summarization.
        embeddings (Any): The embedding model instance.
        user_query (str): The original search query.
        approved_code (str): The final, verified CAPL code.
        functions_called (List[str]): List of functions used in the code.
    """
    logger.info("Saving to Verified Memory... Generating summary...")
    summary_prompt = ChatPromptTemplate.from_messages([
        ("system", """Analyze this CAPL test case. Output a concise technical intent and a test_category.

            CRITICAL RULES:
            1. Keep the intent to ONE single sentence. Maximum 150 characters.
            2. DO NOT repeat words, phrases, or acronym expansions.
            3. Be direct (e.g., 'Verifies VIN reading via UDS service 0x22').
            """),
        ("human", "Code:\n{code}")
    ])
    structured_llm = llm.with_structured_output(MemorySummary, method="json_mode")
    summary_chain = summary_prompt | structured_llm
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            summary = summary_chain.invoke({"code": approved_code})
            break
        except Exception as e:
            print(f"Failed to summarize for memory (Attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                import time
                time.sleep(5)
            else:
                return

    clean_intent = summary.intent
    if len(clean_intent) > 200:
        logger.warning("Warning: LLM stutter detected. Truncating summary.")
        clean_intent = clean_intent[:197] + "..."
        
    search_text = (
        f"Query: {user_query}\n"
        f"Intent: {clean_intent}\n"
        f"Functions Used: {', '.join(functions_called)}\n"
        f"Category: {summary.test_category}"
    )
    
    includes_pattern = re.compile(r'#include\s*["<]([a-zA-Z0-9_.-]+\.cin)[">]', re.IGNORECASE)
    detected_headers = includes_pattern.findall(approved_code)
    memory_vectorstore = Chroma(embedding_function=embeddings, persist_directory=MEMORY_DB_DIR)
    collection = memory_vectorstore._collection 
    
    duplicate_id = None
    try:
        search_vector = embeddings.embed_query(search_text)
        results = collection.query(query_embeddings=[search_vector], n_results=1)
        if results and results.get('distances') and len(results['distances'][0]) > 0:
            distance = results['distances'][0][0]
            matched_id = results['ids'][0][0]
            if distance < 0.2:
                logger.info("Semantic duplicate found (Distance: %.3f). Updating old entry...", distance)
                duplicate_id = matched_id
    except Exception as e:
        logger.warning("Deduplication check skipped: %s", e)
        
    doc_id = duplicate_id if duplicate_id else str(uuid.uuid4())
    memory_doc = Document(
        page_content=search_text,
        metadata={
            "approved_code": approved_code,
            "original_query": user_query,
            "timestamp": int(time.time()),
            "source": "verified_memory",
            "headers_used": ", ".join(detected_headers),
            "doc_id": doc_id 
        }
    )
    
    if duplicate_id:
        memory_vectorstore.delete(ids=[duplicate_id])
    memory_vectorstore.add_documents([memory_doc], ids=[doc_id])
    
    memory_docs = []
    if os.path.exists(MEMORY_BM25_FILE):
        with open(MEMORY_BM25_FILE, 'rb') as f:
            memory_docs = pickle.load(f)
            
    if duplicate_id:
        memory_docs = [doc for doc in memory_docs if doc.metadata.get("doc_id") != duplicate_id]
        
    memory_docs.append(memory_doc)
    with open(MEMORY_BM25_FILE, 'wb') as f:
        pickle.dump(memory_docs, f)
        
    if duplicate_id:
        logger.info("Existing test case updated successfully in Verified Memory.")
    else:
        logger.info("New test case successfully saved to Verified Memory.")


# ---------------------------------------------------------
# Retrieval & LLM Generation
# ---------------------------------------------------------
def generate_capl_code_fast(
    vectorstore: Any, 
    bm25_retriever: Any, 
    memory_vectorstore: Any, 
    memory_bm25: Any, 
    llm: Any, 
    user_query: str,
    master_can_file: Path
) -> Tuple[str, List[str]]:
    """
    Main execution chain: Retrieves context, validates LLM output, and enforces CAPL rules using LangGraph.

    Args:
        vectorstore (Any): The Chroma DB instance for standard snippets.
        bm25_retriever (Any): The BM25 instance for keyword search.
        memory_vectorstore (Any): Chroma DB instance for saved, verified queries.
        memory_bm25 (Any): BM25 instance for verified queries.
        llm (Any): The LangChain LLM to use.
        user_query (str): The user's input requirement.
        master_can_file (Path): Path to the global variables definitions.

    Returns:
        Tuple[str, List[str]]: The final generated code and the list of functions it calls.
    """
    queries = expand_query_multi_way(llm, user_query)
    vector_retriever = vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": 8})
    bm25_retriever.k = 4
    ensemble_retriever = EnsembleRetriever(
        retrievers=[bm25_retriever, vector_retriever],
        weights=[0.4, 0.6]
    )

    logger.info("Executing Global Hybrid Search...")
    filtered_docs = retrieve_hybrid_parallel(ensemble_retriever, queries)
    context_blocks = []
    for doc in filtered_docs:
        source_file = doc.metadata.get('source', 'Unknown_File.cin')
        context_blocks.append(f"/* --- SOURCE: {source_file} --- */\n{doc.page_content}")

    context = "\n\n".join(context_blocks)
    global_vars_dict = extract_global_variables(master_can_file) or "/* No global variables found in template */"
    
    memory_context = ""
    if memory_vectorstore and memory_bm25:
        logger.info("Checking Verified Memory for past examples...")
        mem_vec_retriever = memory_vectorstore.as_retriever(search_kwargs={"k": 2})
        memory_bm25.k = 2  
        mem_ensemble = EnsembleRetriever(
            retrievers=[memory_bm25, mem_vec_retriever],
            weights=[0.5, 0.5]
        )

        mem_docs = retrieve_hybrid_parallel(mem_ensemble, queries)
        if mem_docs:
            mem_blocks = []
            for doc in mem_docs:
                raw_code = doc.metadata.get('approved_code', '')
                clean_code = re.sub(r'includes\s*\{[^}]*\}', '', raw_code, flags=re.IGNORECASE).strip()
                intent = doc.page_content.split('Intent: ')[-1].split('\n')[0]
                mem_blocks.append(f"/* Past Test Intent: {intent} */\n{clean_code}")

            memory_context = "\n\n".join(mem_blocks)
            logger.info("Found %d relevant past test cases.", len(mem_docs))

    CAPL_BUILT_INS = {
        "write", "testWaitForTimeout", "testWaitForTimeoutSilent",
        "testStep", "testStepFail", "testStepPass", "sysSetVariable",
        "sysGetVariable", "setSignal", "getSignal", "testWaitForDiagRequestSent",
        "testWaitForDiagResponse", "diagGetLastResponseCode",
        "DiagRequest_via_bytes_WithFixedLengthReturnType"
    }
    allowed_functions = list(CAPL_BUILT_INS)
    function_to_file_map = {}

    for doc in filtered_docs:
        if 'exports' in doc.metadata and doc.metadata['exports']:
            exported_funcs = [fn.strip() for fn in doc.metadata['exports'].split(',')]
            allowed_functions.extend(exported_funcs)

            source_file = doc.metadata.get('source', '')
            if source_file.lower().endswith('.cin'):
                for fn in exported_funcs:
                    function_to_file_map[fn] = source_file

    dependency_cheat_sheet = "\n".join(
        [f"- {func} -> requires #include \"{file}\"" for func, file in function_to_file_map.items()]
    ) or "/* No .cin dependencies detected in retrieved context */"

    logger.debug("\n" + "=" * 40)
    logger.debug("TRIPWIRE 1: THE CHEAT SHEET")
    logger.debug("=" * 40)
    logger.debug("Final Cheat Sheet sent to LLM: \n%s", dependency_cheat_sheet)
    logger.debug("=" * 40 + "\n")

    workflow = StateGraph(CodeGenState)
    workflow.add_node("generate", node_generate_code)
    workflow.add_node("validate", node_validate_code)

    workflow.set_entry_point("generate")
    workflow.add_edge("generate", "validate")
    workflow.add_conditional_edges("validate", route_next_step)

    app = workflow.compile()

    initial_state = {
        "user_query": user_query,
        "context": context,
        "memory_context": memory_context,
        "dependency_cheat_sheet": dependency_cheat_sheet,
        "global_variables": global_vars_dict,
        "allowed_functions": allowed_functions,
        "messages": [], 
        "attempt": 0,
        "max_retries": 2,
        "is_valid": False,
        "fallback_triggered": False,
        "llm": llm
    }
    logger.info("Starting Agentic Generation Workflow...")
    
    final_state = app.invoke(initial_state)

    if final_state.get("is_valid"):
        return final_state["final_code"], final_state["funcs_called"]
    elif final_state.get("fallback_triggered"):
        return final_state["fallback_code"], []
    else:
        logger.warning("[!] Max retries reached without passing validation.")

        last_result = final_state.get("result")

        if last_result and hasattr(last_result, 'capl_code'):
            broken_code = re.sub(r'includes\s*\{[^}]*\}', '', last_result.capl_code, flags=re.IGNORECASE)

            error_header = (
                "/* =========================================================================\n"
                "   ⚠️ VALIDATION FAILED (MAX RETRIES REACHED)\n"
                "   =========================================================================\n"
                "   The LLM generated this code, but it failed syntax or hallucination checks.\n"
                "   Press [e] to view and fix the errors manually, or [r] to discard.\n"
                "   ========================================================================= */\n\n"
            )
            return error_header + broken_code.strip(), []
        else:
            return "/* ERROR: Model completely failed to generate JSON after maximum retries. */", []
