# ORBIT Projects: Full Product Brief

## Part 1 — Product & UX

### 1. What is ORBIT Projects?

ORBIT is an AI-powered development workspace where users can bring their existing software projects or create new ones, understand their codebases, plan tasks, and work with AI agents.

The Projects section is where users manage their projects and enter the workspace for each one.

The experience should make it easy for a user to answer three questions:
- What projects do I have?
- What is happening inside a particular project?
- How can I use ORBIT to move that project forward?

ORBIT should feel like a practical, well-designed software product. Avoid unnecessary futuristic effects or making the interface look like a conventional IDE with an AI chat panel attached.

The goal is to design the best workflow for the product, not to force the product into a predetermined layout.

### 2. Project creation and importing

Users should be able to bring projects into ORBIT in different ways.

Supported entry points:
- GitHub repositories
- GitLab repositories
- Bitbucket repositories
- Local project folders
- ZIP files and individual file uploads
- Existing projects that users want to continue working on
- New projects created entirely inside ORBIT

Users must not be required to connect GitHub or any other external service to use ORBIT. For example, a beginner should be able to create a new project inside ORBIT without having a GitHub account.

Figma should explore how to make these different entry points easy to discover without overwhelming the interface.

### 3. The Projects page

The Projects page should give users a clear overview of their projects.

Projects are displayed in a grid, with AI-generated summaries that help users understand what each project does. A project could display relevant information such as:
- Project name
- AI-generated description
- Technology or programming languages used
- Project status or recent activity, where useful
- A way to open the project workspace

These are examples of useful information, not a requirement to display every item on every card. The interface should prioritize readability and useful information over decorative elements.

Figma should decide the best card design, information hierarchy, spacing, filters, and organizational structure.

**Known requirements for this page:**
- A search bar
- A status filter: Live, Build in Progress, Archived, Saved Ideas

**Creating a project:** Users need an obvious way to start a new project or import an existing one. Figma should determine whether these actions belong in a primary button, a creation dialog, a dedicated screen, or another appropriate pattern. The design should accommodate both first-time users and people managing multiple projects.

### 4. The individual project workspace

This is the most important part of the brief. When a user opens a project, they should enter a workspace designed around understanding the project and getting work done with AI. Three important concepts:

**A. Project Map** — helps users understand the work associated with their project. It should communicate relevant relationships between project tasks, components, or stages of work. We have not finalized exactly how the map should look or how it should represent these relationships. Figma should explore the most useful representation rather than automatically assuming it needs to be a node graph. The map must serve a practical purpose: helping users understand the project and navigate its work.

**B. Task Details** — users need a way to inspect a task and understand what it involves. Task Details should provide relevant information about the selected task, potentially including its description, requirements, status, progress, and associated work. The information shown should depend on what helps users understand and manage a task. Figma should determine how Task Details relates to the Project Map and the rest of the workspace — e.g. whether task information belongs in a side panel, a dedicated view, or another suitable arrangement.

**C. Main Agent conversation** — the project workspace should include a dedicated conversation with the Main Agent, the primary AI assistant users work with in the context of their project. Users should be able to discuss their project, ask questions, plan work, and request assistance without losing their understanding of the current project. The conversation should be context-aware, centered on the project rather than a disconnected general-purpose chat.

AI interaction should not be restricted to the Main Agent conversation — the workspace should also support interaction associated directly with the work being viewed, such as a selected task. Figma should determine how these two interaction styles coexist naturally without creating duplicate controls or confusing users.

### 5. A separate Agents page

Outside the project workspace, a separate **Agents** page shows a grid of specialized agent types/personas (e.g. a coding agent, a research agent, a debug agent) rather than one agent per project. Users pick the type of agent suited to what they want to build, independent of which project they later point it at.

### 6. Navigation and overall experience

Leaning toward left-side navigation, though the exact arrangement is still open to design exploration. The interface should make it easy to understand:
- Where the user is
- Which project is open
- How to access relevant project information
- How to move between project-level and task-level work
- How to interact with the Main Agent

Figma should determine the most effective layout, navigation hierarchy, and responsive behavior. Avoid adding unrelated features simply to fill empty space.

### 7. User experience requirements

The design should work for different types of users, including beginners who may not understand complex development terminology. Priorities:
- **Clarity** — users should understand what they are looking at
- **Usability** — common actions should be easy to find
- **Context** — users should understand how tasks relate to their project
- **AI integration** — AI should be part of the workflow, not an unrelated widget
- **Scalability** — the interface should remain usable as users acquire more projects and tasks
- **Consistency** — navigation, controls, and interactions should behave predictably
- **Responsiveness** — the workspace should adapt to different screen sizes where practical

Empty states matter. A user with no projects should receive a clear path to create or import one rather than an empty grid with no guidance.

---

## Part 2 — Visual Identity

This section is the brand layer to apply on top of whatever structure comes out of Part 1. It constrains color, type, iconography, and component craft — it does not dictate layout.

### Name & wordmark

- Product name: **orbit** — always lowercase in the wordmark (nav, header, favicon, any branding surface). Not "ORBIT" or "Orbit."
- This lowercase rule applies to the visual wordmark only, not to the agent's own spoken/chat replies.

### Logo / mark

An orbit-rings motif: a central node with crossing elliptical orbit rings and a satellite dot. Original design — not a literal copy of any stock icon.

### Color

Neutral-first palette with a single accent, used in both a dark theme (primary) and a light theme:

- **Base neutrals:** white, black, grey, cream
  - Light theme: cream/white surfaces, near-black text, grey for secondary text and borders
  - Dark theme: near-black/charcoal surfaces (never blue-tinted), off-white text, grey for secondary text and borders
- **Accent:** copper / burnt amber — a single accent color, used sparingly and deliberately: primary CTAs, active states, the orbit mark, key status highlights. Not spread across the whole interface.
- **No blue.** Blue was explored and dropped — it kept bleeding into backgrounds and competing with the accent instead of supporting it.

### Typography

- **Space Grotesk** — wordmark, section headers, card titles
- **Inter** — body copy, chat/message text, labels, buttons, secondary/meta text
- Clear weight distinction between heading and body — avoid flattening everything to one weight.

### Component craft

- One consistent outline icon set throughout — same stroke weight, same corner rounding on every icon, never mixed styles
- Card/panel corner radius standardized (~16px) across chat bubbles, side-rail items, dashboard tiles, and modals
- Soft drop-shadow elevation instead of hard 1px borders
- Generous internal padding — nothing flush against card edges
- Three-tier text hierarchy per card: bold primary label → muted secondary line → value/status set apart (right-aligned or visually distinct)

### Closest full-screen reference

A dark-themed dashboard with left icon+label sidebar nav, a bold large-type status headline near the top, and list items with avatar stacks for scheduled/recent items is the closest existing reference for how navigation, hierarchy, and a warm accent-on-near-black execution should feel at full-screen scale. Swap that reference's orange for our copper, and replace its hand-drawn illustration style and gamification elements (XP, streaks) with our own plain outline icon set — those are specific to that reference's brand, not ours.

### Chat-specific behavior (Main Agent conversation)

- User messages right-aligned, Orbit's left-aligned — distinguished by side/alignment only, no "YOU:"/"ORBIT:" labels
- Agent-mode narration labels which model answered using moon names (Titan primary, Europa fallback; reserved: Io, Ganymede, Callisto, Triton, Luna) — Agent mode only, not shown in plain Chat mode
- A "thinking level" control (Quick vs Deep), separate from the Chat/Agent mode toggle
- Follow-up suggestion chips are generated from the actual reply content only — never shown if a real match can't be produced; placed inline at the end of the agent's message, in the accent color
- Strict message order: user message → thinking indicator → reply; streaming cursor inline on the live text, never a separately floating element
- Auto-scroll to the latest message only when the user is already scrolled to the bottom
- Inline retry on a failed or incomplete message
- Code blocks render with syntax highlighting and a hover copy button
