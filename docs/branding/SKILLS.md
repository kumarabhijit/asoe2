# Website Development Skills Guide

## Purpose

This document defines the **skills, patterns, and design principles** a web
developer should follow when building the company website. It is inspired by
premium stealth-mode AI startup aesthetics (e.g., vibrium.ai) and optimized
for credibility, clarity, and conversion.

---

## 1. Design Philosophy

### Core Principles

| Principle            | Description                                                                 |
|----------------------|-----------------------------------------------------------------------------|
| **Stealth Elegance** | Minimal, confident design that signals sophistication without over-explaining |
| **Dark-First**       | Dark backgrounds with high-contrast typography and selective color accents    |
| **Purposeful Motion**| Subtle animations that guide attention, never distract                       |
| **Enterprise Trust** | Clean layout, consistent spacing, and professional tone that signals B2B readiness |
| **Mobile-First**     | Responsive from the ground up; every section must work on 360px–2560px      |

### Design Personality

```
Confident  ──────────────── not Aggressive
Minimal    ──────────────── not Empty
Technical  ──────────────── not Intimidating
Premium    ──────────────── not Flashy
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

## 3. Page Sections & Layout

### Required Sections (in order)

#### 3.1 Navigation Bar
- Fixed/sticky, transparent on hero, solid on scroll
- Logo (left) + nav links (center or right) + single CTA button (right)
- Mobile: hamburger menu with slide-in panel
- Max 5 nav items: `Product` · `Solutions` · `Company` · `Resources` · `Pricing`
- Height: 64px desktop, 56px mobile

#### 3.2 Hero Section
- Full viewport height (100vh) or near-full (90vh)
- Single powerful headline (6–10 words max)
- One supporting line (15–25 words)
- Primary CTA button + optional secondary (ghost) button
- Optional: subtle animated background (particles, mesh gradient, or grid)
- No stock photos — use abstract visuals or product screenshots

#### 3.3 Social Proof Bar
- Logos of clients, partners, or "backed by" investors
- Grayscale logos, subtle opacity (0.5 → 1.0 on hover)
- Optional: "Trusted by X+ enterprises" tagline

#### 3.4 Problem / Pain Point Section
- 2–3 short pain-point cards or a single narrative block
- Keep it empathetic, not fear-based
- Transition into "how we solve this"

#### 3.5 Product / Solution Section
- Visual product overview (screenshot, illustration, or diagram)
- 3–4 feature cards with icons
- Each card: icon + title (3–5 words) + description (1–2 lines)

#### 3.6 How It Works
- 3-step or 4-step horizontal flow
- Numbered steps with icons/illustrations
- Keep descriptions to one sentence each

#### 3.7 Key Metrics / Results
- 3–4 large numbers with labels
- Example: "1M+ interactions" · "25+ enterprise clients" · "<500ms latency"
- Animated count-up on scroll-into-view

#### 3.8 Testimonials / Case Studies (optional for stealth)
- If in stealth: use anonymized quotes or skip
- Card layout with quote, name, role, company

#### 3.9 CTA Section
- Full-width dark or gradient background
- Compelling headline + primary CTA
- Optional: email capture for waitlist

#### 3.10 Footer
- Columns: Product · Company · Resources · Legal
- Social links (LinkedIn, Twitter/X)
- Copyright notice
- Links: Privacy Policy · Terms of Service · Cookie Policy

---

## 4. Component Patterns

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

- **Page load:** Fade-up staggered (elements appear sequentially, 50ms delay each)
- **Scroll:** Fade-in-up with `IntersectionObserver`, trigger at 20% visibility
- **Hover:** Scale 1.02 + shadow lift, 200ms ease-out
- **Background:** Optional subtle particle field or animated mesh gradient
- **Counters:** Count-up animation on metrics section
- **Duration:** 200ms–400ms for micro-interactions, 600ms–800ms for scroll reveals
- **Easing:** `cubic-bezier(0.16, 1, 0.3, 1)` for smooth deceleration

---

## 5. Imagery & Media

### Rules

- No generic stock photography
- Prefer: abstract 3D renders, gradient meshes, product screenshots, diagrams
- All images: WebP format, lazy-loaded, with `alt` text
- Hero media: consider a looping short video or animated SVG
- Client logos: SVG, monochrome, consistent height (24px–32px)

---

## 6. Performance Targets

| Metric                  | Target         |
|-------------------------|----------------|
| Lighthouse Performance  | 90+            |
| First Contentful Paint  | < 1.5s         |
| Largest Contentful Paint| < 2.5s         |
| Cumulative Layout Shift | < 0.1          |
| Total Page Weight       | < 1.5MB        |
| Time to Interactive     | < 3.5s         |

---

## 7. Accessibility

- WCAG 2.1 AA minimum
- All interactive elements keyboard-navigable
- Contrast ratio: 4.5:1 for body text, 3:1 for large text
- `aria-label` on all icon-only buttons
- Focus rings visible on keyboard navigation
- `prefers-reduced-motion` respected for all animations
- `prefers-color-scheme` supported if light theme exists

---

## 8. SEO & Meta

- Semantic HTML5 (`header`, `main`, `section`, `footer`, `nav`)
- Open Graph + Twitter Card meta tags on every page
- Structured data (Organization, WebSite, FAQ where applicable)
- Canonical URLs
- Sitemap.xml + robots.txt
- Page titles: `{Page} — {Company Name}`
- Meta descriptions: 150–160 characters, action-oriented
