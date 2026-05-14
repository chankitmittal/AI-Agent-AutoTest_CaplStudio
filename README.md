# Automotive Bootloader AI Specialist

## 📌 Project Overview
This repository showcases a specialized AI Agent, engineered to act as a Subject Matter Expert (SME) for **Automotive Bootloader Development and Validation**. 

The agent streamlines the V&V (Validation & Verification) process by converting natural language requirements or protocol definitions into **structured JSON test specifications**. It is designed to be **OEM-agnostic**, using a placeholder-based architecture to maintain data privacy while adhering to international standards.

The prompt used here is independent of the LLM. I have tried this in Gemini.

## 🛠 Core Capabilities
- **UDS Protocol Intelligence:** Deep understanding of ISO 14229 (UDS) services, including Diagnostic Session Control (0x10), Security Access (0x27), and Data Transfer (0x34, 0x36, 0x37).
- **Automated Test Spec Generation:** Transforms complex flashing sequences into machine-readable JSON format.
- **Edge Case Identification:** Proactively suggests test cases for Negative Response Codes (NRCs) such as `0x78` (Pending), `0x24` (Request Sequence Error), and `0x31` (Request Out Of Range).
- **Safety-First Design:** Enforces state-machine logic (e.g., ensuring Security Access is cleared before attempting Memory Erase).

## 📂 Repository Structure
- `Bootloader_Helper_Instructions.md`: The full instruction set and logic constraints.

## 🚀 Design Philosophy: "Portable & Secure"
To comply with strict automotive cybersecurity and copyright standards, this agent is built on a **Zero-Document Architecture**:
1. **No Proprietary Data:** No OEM-specific manuals or confidential documents were used in training or prompting.
2. **XX Placeholder Logic:** All project-specific values (Memory Addresses, CAN IDs, Security Keys) use `XX` placeholders, allowing the user to inject project-specific data without exposing IP.
3. **Standard-Centric:** Logic is derived strictly from **ISO 14229** public standards.

## 📊 Example Workflow
1. **Input:** "Generate a test sequence for a successful software download starting from Default Session."
2. **Agent Reasoning:** Validates the transition from `0x10 0x01` -> `0x10 0x03` -> `0x27` -> `0x34`.
3. **Output:** A structured JSON file containing step-by-step requests, expected responses, and timing constraints (P2/P2*).

## 🗺 Roadmap
- [x] Initial Instruction Engineering & Persona Definition.
- [x] JSON Schema Standardization.
- [ ] **Next Phase:** Integration with Python/CAPL scripts to auto-generate `.can` files for Vector CANoe.

## ⚖ License
Distributed under the MIT License. See `LICENSE` for more information.

---
**Author:** Chankit Mittal
**Focus:** Automotive Embedded Software | AI for Engineering | V&V Automation
