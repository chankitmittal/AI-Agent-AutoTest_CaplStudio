# Software Requirement to CAPL Generation

## 📌 Project Overview
This repository contains project for CAPL Generation from SW requirements. 

The agent streamlines the V&V (Validation & Verification) process by converting natural language requirements or protocol definitions into **structured JSON test specifications**. It is designed to be **OEM-agnostic**, using a placeholder-based architecture to maintain data privacy while adhering to international standards.

Generated Test specifications (*.JSON) will be used to generate CAPL Scripts.

## 🛠 Core Capabilities
- **Automated Test Spec Generation:** Transforms SW requirements into machine-readable JSON format test specification.
- **Automated CAPL Generation:** Generate CAPL scripts from Test specifications. 
- **UDS Protocol Intelligence:** Deep understanding of ISO 14229 (UDS) services, including Diagnostic Session Control (0x10), Security Access (0x27), and Data Transfer (0x34, 0x36, 0x37).

## 📂 Repository Structure
- `SystemPrompt`: This folder contain system prompts which are used to transform SW requirements into Test specification based on the domain.
- `ChromaDB`: This folder contain generated Embedded which will be feed as input for CAPL Generation.

## 📊 Example Workflow
1. **Input:** "SW Requirements like Generate Sequence for a successful software download Request starting from Default Session."
2. **Agent Reasoning:** Validates the transition from `0x10 0x01` -> `0x10 0x03` -> `0x27` -> `0x34`.
3. **Test Specification Output:** A structured JSON file containing step-by-step requests, expected responses.
4. **CAPL Script Output:** CAN file will be generated from JSON file based on the test sequence.

## 🗺 Roadmap
- [x] CAPL Generation architecture from SW Requirements.
- [x] Concept splitted into three phases. First Phase, CAPL Generation from user query; Second Phase, Test Specification generation from SW requirements; Final Phase, Integration of both Phase 1 and 2.
- [x] Phase 1: CAPL Code generation from User query
- [ ] Phase 2: Test specification generation from SW requirements
- [ ] **Final Phase:** Integration of Phase 1 & 2 to auto-generate `.can` files for Vector CANoe from SW requirements using GUI.

## ⚖ License
Distributed under the MIT License. See `LICENSE` for more information.

---
**Author:** Chankit Mittal
**Focus:** Automotive Embedded Software | AI for Engineering | V&V Automation
