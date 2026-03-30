# Website Development Skills Guide

## Purpose

This document defines the **skills, patterns, and design principles** a web
developer should follow when building the company website. It draws from
premium enterprise AI startup aesthetics (vibrium.ai, eudia.com) and is
optimized for credibility, clarity, and conversion in the **CPG domain**.

> **Reference sites:**
> - [vibrium.ai](https://www.vibrium.ai/) — Stealth-mode enterprise agentic AI (voice bots)
> - [eudia.com](https://www.eudia.com/) — Enterprise AI for legal teams ("The Enterprise Brain")
>
> We adapt the best of both: Vibrium's dark, minimal stealth aesthetic combined
> with Eudia's enterprise storytelling depth, named product concepts, and
> solutions-by-vertical architecture — all reframed for **CPG order-to-cash**.

---

## 1. Design Philosophy

### Core Principles

| Principle                  | Description                                                                 |
|----------------------------|-----------------------------------------------------------------------------|
| **Stealth Elegance**       | Minimal, confident design that signals sophistication without over-explaining |
| **Dark-First**             | Dark backgrounds with high-contrast typography and selective color accents    |
| **Purposeful Motion**      | Subtle, delightful animations that guide attention, never distract (Eudia-style polish) |
| **Enterprise Trust**       | Clean layout, consistent spacing, and professional tone that signals Fortune 500 readiness |
| **Domain Authority**       | Every section should reinforce CPG expertise — use industry language, not generic AI talk |
| **Augmented Intelligence** | Position AI as working *with* humans, not replacing them (Eudia's core philosophy) |
| **Mobile-First**           | Responsive from the ground up; every section must work on 360px–2560px      |

### Design Personality

```
Confident     ──────────────── not Aggressive
Minimal       ──────────────── not Empty
Technical     ──────────────── not Intimidating
Premium       ──────────────── not Flashy
Domain-Expert ──────────────── not Generic
Augmenting    ──────────────── not Replacing
```

---

## 2. Visual Language

### Color Palette

#### Primary (Dark Theme — Default)

| Token                | Value         | Usage                              |
|----------------------|---------------|------------------------------------|
| `--bg-primary`       | `#0A0A0F`     | Page background                    |
| `--bg-secondary`     | `#111118`     | Card / section background          |
| `--bg-elevated`      | `#1A1A24`     | Hover states, modals               |
| `--text-primary`     | `#F5F5F7`     | Headings, primary text             |
| `--text-secondary`   | `#8E8E9A`     | Body text, descriptions            |
| `--text-muted`       | `#5A5A6E`     | Captions, metadata                 |
| `--accent-primary`   | `#6C5CE7`     | Primary CTA, links, active states  |
| `--accent-glow`      | `#7C6CF7`     | Hover glow, gradient highlights    |
| `--accent-secondary` | `#00D2FF`     | Secondary accents, data highlights |
| `--border-subtle`    | `#1E1E2E`     | Dividers, card borders             |
| `--success`          | `#00E676`     | Positive states                    |
| `--warning`          | `#FFD600`     | Caution states                     |
| `--error`            | `#FF5252`     | Error states                       |

#### Light Theme (Optional — Accessible Mode)

| Token                | Value         | Usage                              |
|----------------------|---------------|------------------------------------|
| `--bg-primary`       | `#FAFAFA`     | Page background                    |
| `--bg-secondary`     | `#FFFFFF`     | Card / section background          |
| `--text-primary`     | `#111118`     | Headings                           |
| `--text-secondary`   | `#4A4A5A`     | Body text                          |
| `--accent-primary`   | `#5A4BD6`     | CTA, links                         |

### Gradients

```css
/* Hero gradient overlay */
--gradient-hero: linear-gradient(135deg, #6C5CE7 0%, #00D2FF 100%);

/* Subtle card glow */
--gradient-glow: radial-gradient(ellipse at center, rgba(108, 92, 231, 0.15) 0%, transparent 70%);

/* Text gradient for hero headings */
--gradient-text: linear-gradient(90deg, #F5F5F7 0%, #6C5CE7 50%, #00D2FF 100%);
```

### Typography

| Element        | Font Family              | Weight | Size (Desktop) | Size (Mobile) | Line Height |
|----------------|--------------------------|--------|----------------|---------------|-------------|
| H1 (Hero)      | Inter / Satoshi          | 700    | 64px–80px      | 36px–44px     | 1.05        |
| H2 (Section)   | Inter / Satoshi          | 600    | 40px–48px      | 28px–32px     | 1.15        |
| H3 (Card)      | Inter / Satoshi          | 600    | 24px–28px      | 20px–22px     | 1.25        |
| Body           | Inter                    | 400    | 16px–18px      | 15px–16px     | 1.6         |
| Caption        | Inter                    | 400    | 13px–14px      | 12px–13px     | 1.5         |
| Code/Mono      | JetBrains Mono / Fira    | 400    | 14px           | 13px          | 1.5         |
| CTA Button     | Inter                    | 600    | 15px–16px      | 14px–15px     | 1.0         |

**Rules:**
- Max 2 font families (1 sans-serif + 1 monospace)
- Use `font-display: swap` for web fonts
- Letter-spacing: `-0.02em` for headings, `0` for body

### Spacing System

Use an 8px base grid:

```
4px   — micro (icon gaps)
8px   — xs
16px  — sm (inline padding)
24px  — md (card padding)
32px  — lg
48px  — xl (section gap)
64px  — 2xl
96px  — 3xl (section vertical padding)
128px — 4xl (hero vertical padding)
```

### Border Radius

```
4px  — buttons, pills
8px  — input fields
12px — cards
16px — modals, hero cards
24px — feature callouts
```

---

## 3. Site Architecture (Multi-Page)

Inspired by Eudia's multi-page structure. Unlike a single-page stealth site,
this architecture supports deeper storytelling while keeping stealth discipline.

### Sitemap

```
/                    → Homepage (hero + key sections)
/platform            → Platform overview (the "CPG Brain" concept)
/solutions           → Solutions by vertical / use-case
  /solutions/pricing-discrepancies
  /solutions/promotional-corrections
  /solutions/credit-blocks
  /solutions/duplicate-purchase-orders
/company             → About, mission, team, investors
/resources           → Blog, case studies, whitepapers (gated)
/trust               → Security, compliance, certifications
/contact             → Book a demo / Talk to sales
/careers             → Open roles (if hiring)
```

### Navigation Bar
- Fixed/sticky, transparent on hero, solid on scroll
- Logo (left) + nav links (center) + primary CTA button (right)
- Mobile: hamburger menu with slide-in panel
- Nav items: `Platform` · `Solutions` · `Company` · `Resources` · `Trust`
- Right CTA: **"Book a Demo"** (sales-led, Eudia-style)
- Height: 64px desktop, 56px mobile

---

## 4. Homepage Sections (in order)

#### 4.1 Hero Section
- Full viewport height (100vh) or near-full (90vh)
- Single powerful headline (6–10 words max)
- One supporting line (15–25 words) framing the CPG problem space
- Primary CTA: **"Book a Demo"** + secondary ghost: **"See the Platform"**
- Subtle animated background (neural network graph, mesh gradient, or data-flow grid)
- No stock photos — use abstract visuals, product UI, or data-flow diagrams

**Hero copy pattern (Eudia-inspired):**
```
Headline:  "The Enterprise Brain for CPG Operations"
Subhead:   "AI agents that resolve order-to-cash exceptions —
            pricing, promotions, credits, duplicates — autonomously."
```

#### 4.2 Social Proof Bar
- Logos of clients, partners, or "backed by" investors
- Grayscale logos, subtle opacity (0.5 → 1.0 on hover)
- Tagline: "Trusted by leading CPG enterprises" or "Backed by [Investor Names]"
- If in deep stealth, show only investor logos and client count

#### 4.3 Problem / Pain Point Section
- Title: "CPG order-to-cash is broken" (or similar domain-specific framing)
- 3 pain-point cards specific to CPG:
  - Pricing discrepancies costing millions in margin leakage
  - Manual promotional corrections slowing the cash cycle
  - Credit blocks and duplicate POs creating operational friction
- Empathetic tone, backed by industry statistics where possible

#### 4.4 The "CPG Brain" Concept (Eudia-inspired)
This is the **signature section** — adapted from Eudia's "Enterprise Brain."

- Title: "Your CPG Brain" or "The Intelligence Layer for CPG"
- Visual: animated diagram showing knowledge capture → agent deployment → resolution
- 3 pillars (like Eudia's Capture / Connect / Decide):

| Pillar       | Title                        | Description                                           |
|--------------|------------------------------|-------------------------------------------------------|
| **Capture**  | Codify your playbook         | Captures pricing rules, promotion logic, and exception-handling expertise as structured intelligence |
| **Connect**  | Integrate your systems       | Connects to ERP, TPM, and order management systems for real-time, organization-specific context |
| **Resolve**  | Deploy autonomous agents     | AI agents that classify, route, and resolve exceptions using your rules — not generic models |

#### 4.5 Named Product Concepts
Eudia uses coined terms (MINDs, Sigma, Expert Digital Twins). Create similar
branded capabilities:

| Concept Name (example)    | What It Does                                                  |
|---------------------------|---------------------------------------------------------------|
| **{Brand} Agents**        | Autonomous agents that handle specific exception types         |
| **{Brand} Brain**         | The customer-specific knowledge layer (rules, policies, precedent) |
| **{Brand} Recipes**       | Deterministic, auditable resolution playbooks                  |
| **{Brand} Shadow**        | Compliance engine that approves, blocks, or escalates every action |

*Developer note: Use placeholder names. Founder will finalize the coined terms.*

#### 4.6 Solutions by Use Case
- 4 cards linking to `/solutions/...` pages
- Each card: icon + exception type + one-line outcome
- Example: "Pricing Discrepancies → Resolve in minutes, not days"

#### 4.7 How It Works
- 3-step horizontal flow (Eudia uses a similar pattern):
  1. **Build your Brain** — We codify your team's exception-handling expertise
  2. **Deploy Agents** — Autonomous agents resolve exceptions in real time
  3. **Measure Outcomes** — Full audit trail, compliance log, and ROI dashboard

#### 4.8 Key Metrics / Results
- 3–4 large numbers with labels
- Example: "X+ exceptions resolved" · "Y% faster cycle time" · "$Zm margin recovered" · "N enterprise clients"
- Animated count-up on scroll-into-view
- Pattern: use real metrics when available; placeholder format until then

#### 4.9 Customer Story Spotlight (Eudia-inspired)
Eudia showcases named "Brains" per customer (e.g., "The Duracell Brain").
Adapt this pattern:

- Title: "See the {Customer} Brain in action" (when permission granted)
- Short case study card: challenge → solution → measurable outcome
- If still in stealth: use anonymized version — "A Fortune 500 CPG company..."
- Link to full case study on `/resources`

#### 4.10 Trust & Compliance Section
Eudia has a dedicated Trust Center. Adapt:

- Title: "Enterprise-grade trust"
- 3–4 badges/icons: SOC 2 · GDPR · Data Encryption · Audit Trail
- Link to `/trust` page for full details
- Position near bottom, before final CTA (builds confidence before conversion)

#### 4.11 Final CTA Section
- Full-width dark or gradient background
- Headline: "Ready to build your CPG Brain?"
- Primary CTA: **"Book a Demo"**
- Secondary: email capture for waitlist (if not yet in sales-led mode)

#### 4.12 Footer
- Columns: `Platform` · `Solutions` · `Company` · `Resources` · `Legal`
- Social links (LinkedIn, Twitter/X)
- Copyright notice
- Links: Privacy Policy · Terms of Service · Cookie Policy · Trust Center
- Optional: "Backed by [Investor Names]" line

---

## 5. Subpage Guidelines

### /platform
- Deep-dive into the "CPG Brain" architecture
- Visual system diagram showing: Data Sources → Brain → Agents → Resolution
- Feature sections for each named concept (Brain, Agents, Recipes, Shadow)
- Use Eudia's pattern: each feature gets a dedicated section with visual + copy

### /solutions/{use-case}
- One page per exception type (pricing, promotions, credits, duplicates)
- Structure: Problem → How we solve it → Agent workflow diagram → Outcome metrics
- Include a mini case study or scenario walkthrough
- CTA: "See how we resolve {exception type}" → Book a Demo

### /company
- Mission statement rooted in a meaningful concept (Eudia uses "eudaimonia")
- Team section (photos optional in stealth; roles + backgrounds)
- Investor / backer logos
- Hiring CTA linking to `/careers`
- Timeline optional: founded → first clients → funding milestones

### /trust
- Dedicated trust center (inspired by Eudia's trust.eudia.com)
- Security practices, certifications, compliance frameworks
- Data handling, encryption, access controls
- Audit trail and traceability overview (ties back to platform architecture)

### /resources
- Blog posts, whitepapers, industry reports
- Gated content (email capture for downloads)
- Case studies with named customers (when permission granted)

---

## 6. Component Patterns

### Buttons

| Type      | Style                                          |
|-----------|-------------------------------------------------|
| Primary   | Filled `accent-primary`, white text, subtle glow on hover |
| Secondary | Ghost/outlined, `accent-primary` border + text  |
| Tertiary  | Text-only link with underline on hover           |

**States:** Default → Hover (lift + glow) → Active (pressed) → Disabled (0.4 opacity)

### Cards

- Background: `bg-secondary` with `border-subtle` 1px border
- Subtle glow on hover: `box-shadow: 0 0 30px rgba(108, 92, 231, 0.1)`
- Padding: 24px–32px
- Border-radius: 12px

### Icons

- Style: Line icons (Lucide, Phosphor, or custom SVG)
- Size: 20px inline, 24px card headers, 32px–40px feature sections
- Color: `text-muted` default, `accent-primary` on active/hover

### Animations

Eudia invests heavily in motion design (they hire for animation expertise).
Prioritize polish here — it differentiates from generic AI startup sites.

- **Page load:** Fade-up staggered (elements appear sequentially, 50ms delay each)
- **Scroll:** Fade-in-up with `IntersectionObserver`, trigger at 20% visibility
- **Hover:** Scale 1.02 + shadow lift, 200ms ease-out
- **Background:** Animated data-flow or neural graph (subtle, not distracting)
- **Hero:** Consider a looping animation showing agent workflow: classify → route → resolve
- **Counters:** Count-up animation on metrics section
- **Diagrams:** Animated connection lines on the "CPG Brain" system diagram
- **Page transitions:** Smooth cross-fade between pages (if SPA/Next.js)
- **Duration:** 200ms–400ms for micro-interactions, 600ms–800ms for scroll reveals
- **Easing:** `cubic-bezier(0.16, 1, 0.3, 1)` for smooth deceleration

---

## 7. Imagery & Media

### Rules

- No generic stock photography
- Prefer: abstract 3D renders, gradient meshes, product UI screenshots, system diagrams
- **Architecture diagrams** are first-class visuals (Eudia uses them prominently)
- Create branded diagrams showing: Data Sources → CPG Brain → Agents → Resolution → Audit
- All images: WebP format, lazy-loaded, with `alt` text
- Hero media: consider a looping animation showing the agent resolution workflow
- Client logos: SVG, monochrome, consistent height (24px–32px)
- **Product screenshots:** Use actual (or high-fidelity mock) UI — not illustrations
- **Iconography for exception types:** Create a consistent icon set for the 4 exception categories

---

## 8. Performance Targets

| Metric                  | Target         |
|-------------------------|----------------|
| Lighthouse Performance  | 90+            |
| First Contentful Paint  | < 1.5s         |
| Largest Contentful Paint| < 2.5s         |
| Cumulative Layout Shift | < 0.1          |
| Total Page Weight       | < 1.5MB        |
| Time to Interactive     | < 3.5s         |

---

## 9. Accessibility

- WCAG 2.1 AA minimum
- All interactive elements keyboard-navigable
- Contrast ratio: 4.5:1 for body text, 3:1 for large text
- `aria-label` on all icon-only buttons
- Focus rings visible on keyboard navigation
- `prefers-reduced-motion` respected for all animations
- `prefers-color-scheme` supported if light theme exists

---

## 10. SEO & Meta

- Semantic HTML5 (`header`, `main`, `section`, `footer`, `nav`)
- Open Graph + Twitter Card meta tags on every page
- Structured data (Organization, WebSite, FAQ where applicable)
- Canonical URLs
- Sitemap.xml + robots.txt
- Page titles: `{Page} — {Company Name}`
- Meta descriptions: 150–160 characters, action-oriented

---

## 11. Tech Stack Recommendation

Based on Eudia-class production quality:

| Layer              | Recommendation                                     |
|--------------------|----------------------------------------------------|
| Framework          | Next.js 14+ (App Router) or Astro for static-first |
| Styling            | Tailwind CSS + CSS custom properties for tokens     |
| Components         | Radix UI or Headless UI for accessible primitives   |
| Animation          | Framer Motion for scroll/page animations            |
| CMS                | Sanity, Contentful, or MDX for blog/resources       |
| Analytics          | PostHog or Plausible (privacy-first)                |
| Deployment         | Vercel or Cloudflare Pages                          |
| Monitoring         | Sentry for error tracking                           |

---

## 12. Design System Deliverables

The web developer should create or receive:

- [ ] Figma design file with all pages and components
- [ ] Design token file (JSON or CSS custom properties)
- [ ] Component library with Storybook documentation
- [ ] Icon set (SVG sprite or individual files)
- [ ] Brand asset package (logos, OG images, favicons)
- [ ] Animation spec document (timing, easing, triggers)
- [ ] Content spreadsheet (all copy, externalized from components)
- [ ] Accessibility audit checklist
