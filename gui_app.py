import os
import sys
import json
import threading
import re
import logging
import customtkinter as ctk
from tkinter import messagebox, filedialog
from pathlib import Path
from typing import Any, Optional, List

# Import required functions and variables from our scripts
from TestSpec_Generator import (
    generate_test_specification, 
    PROMPT_DIR, 
    OUTPUT_DIR,
    LOCAL_LLM_MODEL as TS_LOCAL_MODEL,
    ONLINE_LLM_MODEL as TS_ONLINE_MODEL,
    TestSpecification,
    TestCase,
    TestStep
)
from app_capl_generator import (
    generate_capl_code_fast, 
    ingest_capl_codebase, 
    save_approved_code_to_memory,
    DOCS_DIR, EMBEDDING_MODEL, LLM_LOCAL_MODEL, LLM_ONLINE_MODEL,
    SHARED_DB_DIR, BM25_FILE, MEMORY_DB_DIR, MEMORY_BM25_FILE
)

import pickle
from langchain_ollama import OllamaEmbeddings, ChatOllama
from langchain_chroma import Chroma
from langchain_community.retrievers import BM25Retriever
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv

load_dotenv()

# Configure root logger to output to the logs folder instead of the console
LOGS_DIR = Path("./logs")
LOGS_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOGS_DIR / "app.log"

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(log_file, encoding='utf-8')]
)
logger = logging.getLogger(__name__)

# Redirect all stdout/stderr prints to the logger so we capture LLM HTTP 500 errors and general prints
class LoggerWriter:
    def __init__(self, level):
        self.level = level
    def write(self, message):
        if message.strip() != "":
            self.level(message.strip())
    def flush(self):
        pass

sys.stdout = LoggerWriter(logging.info)
sys.stderr = LoggerWriter(logging.error)
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    """
    Main Application class for the Req to CAPL Test Generator GUI.
    """
    def __init__(self) -> None:
        """
        Initializes the application, sets up the UI, loads domains, and triggers database initialization.
        """
        super().__init__()
        self.title("Req to CAPL Test Generator")
        self.geometry("1100x800")
        
        self.current_test_spec: Optional[TestSpecification] = None
        self.ts_llm_mode = ctk.StringVar(value="Local")
        self.capl_llm_mode = ctk.StringVar(value="Local")
        self.domain_var = ctk.StringVar(value="")
        self.target_file_var = ctk.StringVar(value="req_test_01")
        
        self.vectorstore: Optional[Chroma] = None
        self.bm25_retriever: Optional[BM25Retriever] = None
        self.memory_vectorstore: Optional[Chroma] = None
        self.memory_bm25: Optional[BM25Retriever] = None
        self.embeddings: Optional[OllamaEmbeddings] = None
        
        self.aggregated_capl_code: List[str] = []
        self.can_filename = "Main_Test.can"
        
        self.setup_ui()
        self.load_domains()
        
        # Initialize databases in background
        threading.Thread(target=self.init_databases, daemon=True).start()

    def setup_ui(self) -> None:
        """
        Sets up the main UI components, including the configuration section and tabs.
        """
        self.build_config_section()
        
        # Create tabs
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.tab_req = self.tabview.add("1. Requirements")
        self.tab_spec = self.tabview.add("2. Test Specification")
        self.tab_capl = self.tabview.add("3. CAPL Code")
        
        self.build_req_tab()
        self.build_spec_tab()
        self.build_capl_tab()
        
    def build_config_section(self) -> None:
        """
        Builds the top configuration frame for selecting models and domain.
        """
        config_frame = ctk.CTkFrame(self)
        config_frame.pack(fill="x", padx=10, pady=10)
        
        # TS Engine Selection
        ts_engine_label = ctk.CTkLabel(config_frame, text="Test Spec LLM:")
        ts_engine_label.pack(side="left", padx=5)
        ts_engine_opt = ctk.CTkOptionMenu(config_frame, values=["Local", "Online"], variable=self.ts_llm_mode)
        ts_engine_opt.pack(side="left", padx=5)
        
        # CAPL Engine Selection
        capl_engine_label = ctk.CTkLabel(config_frame, text="CAPL LLM:")
        capl_engine_label.pack(side="left", padx=(20, 5))
        capl_engine_opt = ctk.CTkOptionMenu(config_frame, values=["Local", "Online"], variable=self.capl_llm_mode)
        capl_engine_opt.pack(side="left", padx=5)
        
        # Domain Selection
        domain_label = ctk.CTkLabel(config_frame, text="Domain:")
        domain_label.pack(side="left", padx=(20, 5))
        self.domain_opt = ctk.CTkOptionMenu(config_frame, values=[], variable=self.domain_var)
        self.domain_opt.pack(side="left", padx=5)

    def build_req_tab(self) -> None:
        """
        Builds the Requirements Input Tab UI.
        """
        # Requirements Text
        req_label = ctk.CTkLabel(self.tab_req, text="Software Requirements:")
        req_label.pack(anchor="w", padx=10, pady=(10, 0))
        self.req_textbox = ctk.CTkTextbox(self.tab_req, height=300)
        self.req_textbox.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Generate Button
        btn_frame = ctk.CTkFrame(self.tab_req, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=10)
        
        self.gen_spec_btn = ctk.CTkButton(btn_frame, text="Generate Test Specification", command=self.on_generate_spec)
        self.gen_spec_btn.pack(side="right")
        self.status_label = ctk.CTkLabel(btn_frame, text="Ready", text_color="green")
        self.status_label.pack(side="left")

    def build_spec_tab(self) -> None:
        """
        Builds the Test Specification Generation and Editing Tab UI.
        """
        self.spec_scroll_frame = ctk.CTkScrollableFrame(self.tab_spec)
        self.spec_scroll_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        btn_frame = ctk.CTkFrame(self.tab_spec, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=10)
        
        file_label = ctk.CTkLabel(btn_frame, text="Output File (JSON):")
        file_label.pack(side="left", padx=(0, 5))
        file_entry = ctk.CTkEntry(btn_frame, textvariable=self.target_file_var, width=150)
        file_entry.pack(side="left", padx=5)
        
        self.save_spec_btn = ctk.CTkButton(btn_frame, text="Save Test Spec (JSON)", command=self.on_save_spec, state="disabled")
        self.save_spec_btn.pack(side="left", padx=10)
        
        self.add_tc_btn = ctk.CTkButton(btn_frame, text="Add Manual Test Case", command=self.add_test_case)
        self.add_tc_btn.pack(side="left", padx=10)
        
        self.import_spec_btn = ctk.CTkButton(btn_frame, text="Import JSON", command=self.on_import_spec)
        self.import_spec_btn.pack(side="left", padx=10)
        
        self.gen_capl_btn = ctk.CTkButton(btn_frame, text="Approve & Generate CAPL", command=self.on_generate_capl, state="disabled")
        self.gen_capl_btn.pack(side="right")

    def build_capl_tab(self) -> None:
        """
        Builds the CAPL Generation Tab UI.
        """
        top_frame = ctk.CTkFrame(self.tab_capl, fg_color="transparent")
        top_frame.pack(fill="x", padx=10, pady=5)
        
        query_lbl = ctk.CTkLabel(top_frame, text="Direct Query:")
        query_lbl.pack(side="left", anchor="n", pady=5)
        
        self.direct_query_textbox = ctk.CTkTextbox(top_frame, width=500, height=80)
        self.direct_query_textbox.pack(side="left", padx=5, pady=5)
        
        gen_direct_btn = ctk.CTkButton(top_frame, text="Generate Direct CAPL", command=self.on_generate_direct_capl)
        gen_direct_btn.pack(side="left", padx=5, pady=5)
        
        save_capl_btn = ctk.CTkButton(top_frame, text="Save CAPL File", command=self.on_save_capl, fg_color="green", hover_color="darkgreen")
        save_capl_btn.pack(side="right", padx=5, pady=5)
        
        self.capl_textbox = ctk.CTkTextbox(self.tab_capl, height=500, font=("Consolas", 12))
        self.capl_textbox.pack(fill="both", expand=True, padx=10, pady=10)

    def load_domains(self) -> None:
        """
        Loads the list of available domains from the system prompt directory.
        """
        prompt_dir_path = Path(PROMPT_DIR)
        prompt_dir_path.mkdir(parents=True, exist_ok=True)
        domains = [file.stem for file in prompt_dir_path.glob("*.md")]
        if domains:
            self.domain_opt.configure(values=domains)
            self.domain_var.set(domains[0])
        else:
            self.domain_opt.configure(values=["No domains found"])

    def init_databases(self) -> None:
        """
        Initializes the vector databases (Chroma and BM25) for RAG context gathering.
        Runs in a background thread to prevent UI blocking.
        """
        self.update_status("Initializing vector databases...", "orange")
        self.embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
        
        if not os.path.exists(SHARED_DB_DIR) or not os.path.exists(BM25_FILE):
            logger.info("Shared database missing. Building Vector and BM25 databases...")
            self.vectorstore, all_chunks = ingest_capl_codebase(DOCS_DIR, self.embeddings, SHARED_DB_DIR)
            if all_chunks:
                self.bm25_retriever = BM25Retriever.from_documents(all_chunks)
        else:
            self.vectorstore = Chroma(persist_directory=SHARED_DB_DIR, embedding_function=self.embeddings)
            with open(BM25_FILE, 'rb') as f:
                all_chunks = pickle.load(f)
            self.bm25_retriever = BM25Retriever.from_documents(all_chunks)
            
        if os.path.exists(MEMORY_DB_DIR) and os.path.exists(MEMORY_BM25_FILE):
            self.memory_vectorstore = Chroma(persist_directory=MEMORY_DB_DIR, embedding_function=self.embeddings)
            with open(MEMORY_BM25_FILE, 'rb') as f:
                mem_docs = pickle.load(f)
                if mem_docs:
                    self.memory_bm25 = BM25Retriever.from_documents(mem_docs)
                    
        self.update_status("Databases ready.", "green")

    def update_status(self, text: str, color: str = "white") -> None:
        """
        Updates the status label on the UI.
        
        Args:
            text (str): The status text.
            color (str): The color of the text.
        """
        self.status_label.configure(text=text, text_color=color)

    def get_llm(self, for_spec: bool = True) -> Any:
        """
        Instantiates the LLM instance based on user selection.
        
        Args:
            for_spec (bool): True if requesting the LLM for Test Spec generation, False for CAPL generation.
            
        Returns:
            Any: An instance of a Langchain chat model.
        """
        mode = self.ts_llm_mode.get() if for_spec else self.capl_llm_mode.get()
        if mode == "Online":
            model_name = TS_ONLINE_MODEL if for_spec else LLM_ONLINE_MODEL
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            return ChatGoogleGenerativeAI(model=model_name, temperature=0.0, max_output_tokens=2000, google_api_key=api_key)
        else:
            model_name = TS_LOCAL_MODEL if for_spec else LLM_LOCAL_MODEL
            if for_spec:
                return ChatOllama(model=model_name, temperature=0.0, num_ctx=4092, num_thread=8, mirostat=0)
            else:
                return ChatOllama(model=model_name, temperature=0.0, num_ctx=6000)

    def on_generate_spec(self) -> None:
        """
        Callback handler for generating the test specification from requirements.
        """
        req_text = self.req_textbox.get("1.0", "end").strip()
        if not req_text:
            messagebox.showerror("Error", "Please enter Software Requirements.")
            return
            
        domain = self.domain_var.get()
        if not domain or domain == "No domains found":
            messagebox.showerror("Error", "Please select a valid domain.")
            return
            
        filename = self.target_file_var.get().strip()
        if not filename:
            filename = "req_test_01"
            self.target_file_var.set(filename)

        self.update_status("Generating Test Specification...", "orange")
        self.gen_spec_btn.configure(state="disabled")
        
        def run_gen():
            try:
                llm = self.get_llm(for_spec=True)
                spec = generate_test_specification(req_text, filename, domain, llm)
                self.after(0, self.on_spec_generated, spec)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.after(0, lambda: self.update_status("Generation failed.", "red"))
                self.after(0, lambda: self.gen_spec_btn.configure(state="normal"))

        threading.Thread(target=run_gen, daemon=True).start()

    def on_spec_generated(self, spec: Optional[TestSpecification]) -> None:
        """
        Callback handler triggered when the Test Specification generation is complete.
        
        Args:
            spec (Optional[TestSpecification]): The generated test specification object, or None if failed.
        """
        self.gen_spec_btn.configure(state="normal")
        if not spec:
            self.update_status("Generation failed.", "red")
            return
            
        self.current_test_spec = spec
        self.update_status("Test Specification generated.", "green")
        
        self.tabview.set("2. Test Specification")
        self.render_spec_ui()

    def render_spec_ui(self) -> None:
        """
        Renders the Test Specification editing UI elements dynamically based on the current spec.
        """
        for widget in self.spec_scroll_frame.winfo_children():
            widget.destroy()
            
        self.spec_editors = []
        self.tc_editors = []
        
        if not self.current_test_spec:
            return

        for i, tc in enumerate(self.current_test_spec.test_cases):
            tc_frame = ctk.CTkFrame(self.spec_scroll_frame)
            tc_frame.pack(fill="x", padx=5, pady=10)
            
            t_frame = ctk.CTkFrame(tc_frame, fg_color="transparent")
            t_frame.pack(fill="x", padx=5, pady=5)
            
            title_lbl = ctk.CTkLabel(t_frame, text=f"Test Case {i+1}: ", font=("Arial", 14, "bold"))
            title_lbl.pack(side="left")
            title_entry = ctk.CTkEntry(t_frame, width=300, font=("Arial", 14))
            title_entry.insert(0, tc.title)
            title_entry.pack(side="left", padx=5)
            
            del_tc_btn = ctk.CTkButton(t_frame, text="Delete TC", width=80, fg_color="red", hover_color="darkred", command=lambda idx=i: self.delete_test_case(idx))
            del_tc_btn.pack(side="right")
            
            add_step_btn = ctk.CTkButton(t_frame, text="Add Step", width=80, fg_color="blue", hover_color="darkblue", command=lambda idx=i: self.add_test_step(idx))
            add_step_btn.pack(side="right", padx=5)
            
            for j, step in enumerate(tc.steps):
                step_frame = ctk.CTkFrame(tc_frame, fg_color="transparent")
                step_frame.pack(fill="x", padx=10, pady=2)
                
                act_lbl = ctk.CTkLabel(step_frame, text=f"Step {j+1} Action:", width=100, anchor="w")
                act_lbl.pack(side="left")
                
                act_txt = ctk.CTkTextbox(step_frame, height=40)
                act_txt.insert("1.0", step.action)
                act_txt.pack(side="left", fill="x", expand=True, padx=5)
                
                exp_lbl = ctk.CTkLabel(step_frame, text="Expected:", width=80, anchor="w")
                exp_lbl.pack(side="left", padx=5)
                
                exp_txt = ctk.CTkTextbox(step_frame, height=40)
                exp_txt.insert("1.0", step.expected_response)
                exp_txt.pack(side="left", fill="x", expand=True, padx=5)
                
                del_step_btn = ctk.CTkButton(step_frame, text="X", width=30, fg_color="red", hover_color="darkred", command=lambda tidx=i, sidx=j: self.delete_test_step(tidx, sidx))
                del_step_btn.pack(side="right")

                self.spec_editors.append({
                    "tc_idx": i,
                    "step_idx": j,
                    "act_txt": act_txt,
                    "exp_txt": exp_txt
                })
                
            self.tc_editors.append({
                "tc_idx": i,
                "title_entry": title_entry
            })
        
        self.save_spec_btn.configure(state="normal")
        self.gen_capl_btn.configure(state="normal")

    def delete_test_case(self, tc_idx: int) -> None:
        """
        Deletes a specific test case from the specification.
        
        Args:
            tc_idx (int): The index of the test case to delete.
        """
        self._apply_edits_to_spec()
        if self.current_test_spec and 0 <= tc_idx < len(self.current_test_spec.test_cases):
            del self.current_test_spec.test_cases[tc_idx]
        self.render_spec_ui()

    def delete_test_step(self, tc_idx: int, step_idx: int) -> None:
        """
        Deletes a specific step from a test case.
        
        Args:
            tc_idx (int): The index of the parent test case.
            step_idx (int): The index of the step to delete.
        """
        self._apply_edits_to_spec()
        if self.current_test_spec and 0 <= tc_idx < len(self.current_test_spec.test_cases):
            tc = self.current_test_spec.test_cases[tc_idx]
            if 0 <= step_idx < len(tc.steps):
                del tc.steps[step_idx]
        self.render_spec_ui()

    def add_test_case(self) -> None:
        """
        Adds a new, empty test case to the specification.
        """
        if not self.current_test_spec:
            self.current_test_spec = TestSpecification(sequence_verification="Manual", test_cases=[])
            
        if hasattr(self, 'spec_editors') and self.spec_editors:
            self._apply_edits_to_spec()
            
        self.current_test_spec.test_cases.append(TestCase(
            title=f"Manual_Test_{len(self.current_test_spec.test_cases)+1}",
            type="Positive",
            preconditions=[],
            steps=[],
            postconditions=[]
        ))
        self.tabview.set("2. Test Specification")
        self.render_spec_ui()

    def add_test_step(self, tc_idx: int) -> None:
        """
        Adds a new, empty step to a specific test case.
        
        Args:
            tc_idx (int): The index of the test case to modify.
        """
        self._apply_edits_to_spec()
        if self.current_test_spec and 0 <= tc_idx < len(self.current_test_spec.test_cases):
            self.current_test_spec.test_cases[tc_idx].steps.append(TestStep(action="", expected_response=""))
        self.render_spec_ui()

    def _apply_edits_to_spec(self) -> None:
        """
        Reads the user's edits from the UI components and updates the internal TestSpecification object.
        """
        if not self.current_test_spec: 
            return
        
        for edit in getattr(self, 'tc_editors', []):
            tc_idx = edit["tc_idx"]
            if tc_idx < len(self.current_test_spec.test_cases):
                self.current_test_spec.test_cases[tc_idx].title = edit["title_entry"].get().strip()
                
        for edit in getattr(self, 'spec_editors', []):
            tc_idx = edit["tc_idx"]
            step_idx = edit["step_idx"]
            new_action = edit["act_txt"].get("1.0", "end").strip()
            new_exp = edit["exp_txt"].get("1.0", "end").strip()
            
            self.current_test_spec.test_cases[tc_idx].steps[step_idx].action = new_action
            self.current_test_spec.test_cases[tc_idx].steps[step_idx].expected_response = new_exp

    def on_save_spec(self, silent: bool = False) -> None:
        """
        Saves the current Test Specification to a JSON file.
        
        Args:
            silent (bool): If True, suppresses the success dialog.
        """
        if not self.current_test_spec: 
            return
            
        self._apply_edits_to_spec()
        
        filename = self.target_file_var.get().strip()
        if not filename: 
            filename = "req_test_01"
        if not filename.endswith(".json"): 
            filename += ".json"
        
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        file_path = os.path.join(OUTPUT_DIR, filename)
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(self.current_test_spec.model_dump(), f, indent=4)
            
        if not silent:
            messagebox.showinfo("Saved", f"Test Specification saved to {file_path}")

    def on_import_spec(self) -> None:
        """
        Opens a dialog to import an existing Test Specification JSON file into the application.
        """
        file_path = filedialog.askopenfilename(
            title="Select Test Specification JSON",
            filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
            initialdir=OUTPUT_DIR
        )
        if not file_path:
            return
            
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            imported_spec = TestSpecification(**data)
            self.current_test_spec = imported_spec
            
            filename = os.path.basename(file_path)
            self.target_file_var.set(filename.replace(".json", ""))
            
            self.render_spec_ui()
            self.update_status(f"Successfully imported {filename}", "green")
            
        except Exception as e:
            logger.error("Failed to import JSON file: %s", e)
            messagebox.showerror("Import Error", f"Failed to import JSON file:\n{e}")

    def on_generate_direct_capl(self) -> None:
        """
        Callback handler to bypass the specification generation and directly generate CAPL from a single text query.
        """
        query = self.direct_query_textbox.get("1.0", "end").strip()
        if not query:
            messagebox.showerror("Error", "Please enter a query.")
            return
            
        self.capl_textbox.delete("1.0", "end")
        self.capl_textbox.insert("end", f"/* Processing Direct Query:\n{query}\n*/\n\nGenerating CAPL code, please wait...\n")
        self.direct_query_textbox.delete("1.0", "end")
            
        if not self.vectorstore or not self.bm25_retriever:
            messagebox.showwarning("Warning", "Databases not fully initialized yet. Please wait.")
            return
            
        class DummyTC:
            title = "Direct Query"
            
        tc = DummyTC()
        
        self.update_status("Generating Direct CAPL...", "orange")
        
        def run_gen():
            llm = self.get_llm(for_spec=False)
            master_file_path = Path(DOCS_DIR) / "Global_Variables.cin"
            try:
                final_code, funcs_called = generate_capl_code_fast(
                    vectorstore=self.vectorstore,
                    bm25_retriever=self.bm25_retriever,
                    memory_vectorstore=self.memory_vectorstore,
                    memory_bm25=self.memory_bm25,
                    llm=llm,
                    user_query=query,
                    master_can_file=master_file_path
                )
                final_code = f"/* QUERY:\n{query}\n*/\n" + final_code
            except Exception as e:
                logger.error("Error generating direct CAPL: %s", e)
                final_code = f"/* ERROR generating Direct Query: {e} */"
                funcs_called = []
                
            self.after(0, self.show_review_modal, -1, tc, query, final_code, funcs_called, llm)
            
        threading.Thread(target=run_gen, daemon=True).start()

    def on_generate_capl(self) -> None:
        """
        Callback handler to begin processing the complete test specification and generate CAPL cases.
        """
        if not self.current_test_spec: 
            return
        if not self.vectorstore or not self.bm25_retriever:
            messagebox.showwarning("Warning", "Databases not fully initialized yet. Please wait.")
            return

        self._apply_edits_to_spec()
        self.on_save_spec(silent=True) 
        
        self.tabview.set("3. CAPL Code")
        self.capl_textbox.delete("1.0", "end")
        self.gen_capl_btn.configure(state="disabled")
        
        self.process_next_test_case(0)

    def process_next_test_case(self, index: int) -> None:
        """
        Recursively processes test cases to generate CAPL logic one by one.
        
        Args:
            index (int): The index of the test case currently being processed.
        """
        if self.current_test_spec is None:
            return
            
        if index >= len(self.current_test_spec.test_cases):
            self.on_all_capl_generated()
            return
            
        tc = self.current_test_spec.test_cases[index]
        self.update_status(f"Generating Test {index + 1} of {len(self.current_test_spec.test_cases)}...", "orange")
        
        actions_summary = " Then ".join([s.action.strip() for s in tc.steps if s.action.strip()])
        query = f"Test Case: {tc.title}. Verify by performing the following actions: {actions_summary}"
        
        self.capl_textbox.delete("1.0", "end")
        self.capl_textbox.insert("end", f"/* Processing Test {index + 1} of {len(self.current_test_spec.test_cases)}:\n{query}\n*/\n\nGenerating CAPL code, please wait...\n")
        
        def run_gen():
            if index > 0:
                self.update_status(f"Cooldown: Waiting 3 seconds before Test {index + 1}...", "orange")
                import time
                time.sleep(3)
                self.update_status(f"Generating Test {index + 1} of {len(self.current_test_spec.test_cases)}...", "orange")
                
            llm = self.get_llm(for_spec=False)
            master_file_path = Path(DOCS_DIR) / "Global_Variables.cin"
            
            try:
                final_code, funcs_called = generate_capl_code_fast(
                    vectorstore=self.vectorstore,
                    bm25_retriever=self.bm25_retriever,
                    memory_vectorstore=self.memory_vectorstore,
                    memory_bm25=self.memory_bm25,
                    llm=llm,
                    user_query=query,
                    master_can_file=master_file_path
                )
                final_code = f"/* QUERY:\n{query}\n*/\n" + final_code
            except Exception as e:
                logger.error("Error generating CAPL code for %s: %s", tc.title, e)
                final_code = f"/* ERROR generating {tc.title}: {e} */"
                funcs_called = []
                
            self.after(0, self.show_review_modal, index, tc, query, final_code, funcs_called, llm)

        threading.Thread(target=run_gen, daemon=True).start()

    def show_review_modal(self, index: int, tc: Any, query: str, generated_code: str, funcs_called: List[str], llm: Any) -> None:
        """
        Displays a modal window allowing the user to review, edit, and approve the generated CAPL code.
        
        Args:
            index (int): The current test case index (-1 if direct query).
            tc (Any): The test case object.
            query (str): The search query used to generate the code.
            generated_code (str): The raw output code from the LLM.
            funcs_called (List[str]): List of functions utilized in the code.
            llm (Any): The LLM instance used for summarization memory updates.
        """
        modal = ctk.CTkToplevel(self)
        modal.title(f"Review Test Case {index + 1 if index != -1 else 'Direct Query'}: {tc.title}")
        modal.geometry("900x700")
        modal.transient(self)
        modal.grab_set()
        
        is_error = "/* ERROR" in generated_code or "⚠️ VALIDATION FAILED" in generated_code
        status_color = "red" if is_error else "green"
        status_text = "Partial/Wrong - Please correct the code" if is_error else "Correct - Ready to Accept"
        
        header_lbl = ctk.CTkLabel(modal, text=status_text, text_color=status_color, font=("Arial", 16, "bold"))
        header_lbl.pack(pady=10)
        
        code_textbox = ctk.CTkTextbox(modal, font=("Consolas", 12))
        code_textbox.insert("1.0", generated_code)
        code_textbox.pack(fill="both", expand=True, padx=10, pady=10)
        
        btn_frame = ctk.CTkFrame(modal, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=10)
        
        def on_accept():
            edited_code = code_textbox.get("1.0", "end").strip()
            modal.destroy()
            
            self.update_status(f"Saving Test {index + 1} to memory...", "orange")
            
            def save_and_reload():
                try:
                    save_approved_code_to_memory(
                        llm=llm,
                        embeddings=self.embeddings,
                        user_query=query,
                        approved_code=edited_code,
                        functions_called=funcs_called
                    )
                    self.memory_vectorstore = Chroma(persist_directory=MEMORY_DB_DIR, embedding_function=self.embeddings)
                    with open(MEMORY_BM25_FILE, 'rb') as f:
                        self.memory_bm25 = BM25Retriever.from_documents(pickle.load(f))
                except Exception as e:
                    logger.error("Error saving to memory: %s", e)
                    
                self.aggregated_capl_code.append(edited_code)
                if index != -1:
                    self.after(0, self.process_next_test_case, index + 1)
                else:
                    self.after(0, self.on_all_capl_generated)
                
            threading.Thread(target=save_and_reload, daemon=True).start()

        def on_discard():
            modal.destroy()
            if index != -1:
                self.process_next_test_case(index + 1)
            else:
                self.on_all_capl_generated()

        modal.protocol("WM_DELETE_WINDOW", on_accept)

        accept_btn = ctk.CTkButton(btn_frame, text="Accept & Save", fg_color="green", hover_color="darkgreen", command=on_accept)
        accept_btn.pack(side="right", padx=5)
        
        discard_btn = ctk.CTkButton(btn_frame, text="Discard", fg_color="red", hover_color="darkred", command=on_discard)
        discard_btn.pack(side="right", padx=5)

    def on_all_capl_generated(self) -> None:
        """
        Assembles all generated test blocks into a single CAPL file text.
        """
        self.gen_capl_btn.configure(state="normal")
        
        all_includes = set()
        clean_code_blocks = []
        for code in getattr(self, 'aggregated_capl_code', []):
            includes_match = re.search(r'includes\s*\{([^}]*)\}', code, re.IGNORECASE)
            if includes_match:
                for line in includes_match.group(1).split('\n'):
                    if line.strip():
                        all_includes.add(line.strip())
            clean_code = re.sub(r'includes\s*\{[^}]*\}\s*', '', code, flags=re.IGNORECASE).strip()
            clean_code = re.sub(r'void\s+MainTest\s*\(\)\s*\{[^}]*\}', '', clean_code, flags=re.IGNORECASE).strip()
            clean_code_blocks.append(clean_code)
            
        testcase_names = []
        for code in clean_code_blocks:
            matches = re.findall(r'testcase\s+([a-zA-Z0-9_]+)\s*\(', code, re.IGNORECASE)
            testcase_names.extend(matches)
            
        main_test_block = "void MainTest()\n{\n"
        for name in testcase_names:
            main_test_block += f"  {name}();\n"
        main_test_block += "}\n"
        
        combined_includes = "includes\n{\n  " + "\n  ".join(sorted(list(all_includes))) + "\n}\n\n" if all_includes else ""
        combined_code = combined_includes + "\n\n".join(clean_code_blocks) + "\n\n" + main_test_block
        
        self.capl_textbox.delete("1.0", "end")
        self.capl_textbox.insert("1.0", combined_code)
        self.update_status("All CAPL Tests Generated.", "green")
        messagebox.showinfo("Success", "All tests completed. Press 'Save CAPL File' to commit to disk.")

    def on_save_capl(self) -> None:
        """
        Saves the compiled CAPL test script to a .can file on disk.
        """
        new_code = self.capl_textbox.get("1.0", "end").strip()
        if not new_code: 
            return
        
        capl_dir = Path("./Generated_CAPL_Test")
        capl_dir.mkdir(parents=True, exist_ok=True)
        filepath = capl_dir / getattr(self, 'can_filename', "Main_Test.can")
        
        all_includes = set()
        clean_code_blocks = []
        
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                existing_content = f.read()
            includes_match = re.search(r'includes\s*\{([^}]*)\}', existing_content, re.IGNORECASE)
            if includes_match:
                for line in includes_match.group(1).split('\n'):
                    if line.strip():
                        all_includes.add(line.strip())
            clean_existing = re.sub(r'includes\s*\{[^}]*\}\s*', '', existing_content, flags=re.IGNORECASE).strip()
            clean_existing = re.sub(r'void\s+MainTest\s*\(\)\s*\{[^}]*\}', '', clean_existing, flags=re.IGNORECASE).strip()
            if clean_existing:
                clean_code_blocks.append(clean_existing)
                
        includes_match = re.search(r'includes\s*\{([^}]*)\}', new_code, re.IGNORECASE)
        if includes_match:
            for line in includes_match.group(1).split('\n'):
                if line.strip():
                    all_includes.add(line.strip())
        clean_new = re.sub(r'includes\s*\{[^}]*\}\s*', '', new_code, flags=re.IGNORECASE).strip()
        clean_new = re.sub(r'void\s+MainTest\s*\(\)\s*\{[^}]*\}', '', clean_new, flags=re.IGNORECASE).strip()
        if clean_new:
            clean_code_blocks.append(clean_new)
            
        testcase_names = []
        for code in clean_code_blocks:
            matches = re.findall(r'testcase\s+([a-zA-Z0-9_]+)\s*\(', code, re.IGNORECASE)
            testcase_names.extend(matches)
            
        main_test_block = "void MainTest()\n{\n"
        for name in testcase_names:
            main_test_block += f"  {name}();\n"
        main_test_block += "}\n"
            
        combined_includes = "includes\n{\n  " + "\n  ".join(sorted(list(all_includes))) + "\n}\n\n" if all_includes else ""
        combined_code = combined_includes + "\n\n".join(clean_code_blocks) + "\n\n" + main_test_block
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(combined_code)
            
        self.capl_textbox.delete("1.0", "end")
        self.capl_textbox.insert("1.0", combined_code)
        
        self.aggregated_capl_code = []
        
        messagebox.showinfo("Saved", f"CAPL Code successfully saved and appended to {filepath}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
