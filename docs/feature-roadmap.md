# Feature Roadmap

Phased feature plan for Lyra. Each phase must be polished and stable before moving to the next. No phase is complete until the core flow works flawlessly.

## Phase 1: Foundation (MVP)

**Goal:** Upload documents, get contextual AI help about them. Nothing else.

- [ ] Project scaffolding
  - FastAPI backend with basic routing
  - Next.js frontend with layout shell
  - SQLite database with schema
  - Settings panel (LLM endpoint config, API key, model selection)
  - Test connection button that validates the endpoint and fetches available models

- [ ] Class workspace
  - Create a new class (name, code, semester)
  - Class list view on the home page
  - Class workspace view with document sidebar and main chat area

- [ ] Document upload and ingestion
  - Drag-and-drop PDF/image upload
  - Text-based PDF extraction (PyMuPDF)
  - OCR pipeline for scanned documents (Unlimited OCR)
  - Semantic chunking strategy
  - Local embedding model (nomic-embed-text)
  - Vector storage with sqlite-vec

- [ ] Contextual chat
  - Send a message, get a streaming response
  - Retrieved context injected from uploaded documents
  - Guide/Show toggle (Socratic vs. direct explanation)
  - Markdown rendering in responses

- [ ] Automatic profile extraction
  - Internal analysis pass on document upload
  - Class profile built from syllabus/homework data
  - Profile injected as context for all class interactions

**Definition of done:** A student can create a class, upload a homework PDF, ask about a specific problem, and get a contextual answer that references the uploaded material.

## Phase 2: Study Tools

**Goal:** Add purposeful study features on top of the document + chat foundation.

- [ ] Homework walkthrough mode
  - Upload a homework, AI identifies each problem
  - Step-by-step walkthrough per problem
  - AI asks checking questions between steps
  - Tracks which problems the user struggled with

- [ ] Practice problem generation
  - AI generates new problems based on uploaded homework/notes
  - Configurable difficulty and topic focus
  - User can attempt answers, AI provides feedback

- [ ] Class profile viewer
  - User-visible view of the auto-extracted class profile
  - Editable fields for corrections
  - Deadline calendar view

- [ ] Document viewer
  - In-app PDF/image viewer alongside the chat
  - Highlight text in the document to ask about it
  - Citation links from AI response back to source document

## Phase 3: Knowledge Building

**Goal:** Help students retain what they learn, not just finish homework.

- [ ] Flashcard generation
  - AI generates flashcards from class materials
  - Spaced repetition scheduling
  - Review session within the app

- [ ] Quiz mode
  - AI generates quizzes from specified topics/homeworks
  - Multiple choice, short answer, or problem-solving
  - Score tracking and weakness identification

- [ ] Study guide generation
  - "I have a test on [date] covering [topics]"
  - AI creates a structured study plan
  - Equation sheets, summary notes, key concept reviews

- [ ] User profile refinement
  - AI tracks learning patterns across classes
  - Identifies recurring weak areas
  - Adapts explanation style based on what works

## Phase 4: Advanced Features

**Goal:** Polish and extend based on real usage patterns.

- [ ] Cross-class connections
  - AI references relevant concepts from prerequisite courses
  - "Remember Laplace transforms from Diff Eq?"

- [ ] Email drafting helper
  - "Draft an email to my professor asking about X"
  - Uses class profile for professor contact and tone

- [ ] Bulk document import
  - Canvas/syllabus scraping (user provides credentials or exports)
  - Automatic document organization by type

- [ ] Bundled local LLM
  - Ship with llama.cpp for zero-config local inference
  - Model download and management UI

- [ ] macOS native wrapper
  - Tauri or Electron for native distribution
  - Menu bar presence, notifications for deadlines
  - App Store distribution

## Not on the roadmap (explicitly excluded)

- Multi-user / cloud sync
- Social features or sharing
- Mobile app
- Plugin system
- Voice interaction
- Video lecture processing
