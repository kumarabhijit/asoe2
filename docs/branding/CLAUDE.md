# Branding & Trademark Guidelines — CLAUDE.md

## Purpose

This document defines the **branding, voice, trademark, and legal guidelines**
for the company website. Any developer, designer, or content creator working on
the website must follow these rules to ensure brand consistency and legal
compliance.

---

## 1. Brand Identity

### Brand Positioning

```
We are an enterprise AI platform that delivers measurable business outcomes.
We build autonomous, goal-oriented AI agents that integrate directly into
business workflows — improving revenue, efficiency, and customer experience.
```

### Brand Attributes

| Attribute      | We Are                          | We Are Not                     |
|----------------|----------------------------------|--------------------------------|
| Tone           | Confident, clear, precise        | Hype-driven, jargon-heavy      |
| Personality    | Expert partner                   | Pushy vendor                   |
| Design         | Premium, minimal, dark-first     | Cluttered, colorful, playful   |
| Language        | Direct, outcome-focused          | Vague, buzzword-laden          |
| Audience Focus | Enterprise decision-makers       | General consumers              |

### Tagline Guidelines

- Keep taglines to 6–10 words
- Lead with the outcome, not the technology
- Avoid cliches: "revolutionary", "game-changing", "next-gen", "cutting-edge"

**Good examples:**
- "Autonomous AI agents for enterprise workflows"
- "AI that works. Outcomes you can measure."
- "Deploy intelligent agents. Drive real results."

**Avoid:**
- "The next-generation AI-powered solution for everything"
- "Revolutionizing the future of enterprise with AI"

---

## 2. Voice & Tone

### Writing Principles

1. **Be direct.** Say what you mean in the fewest words possible.
2. **Be specific.** Use numbers, outcomes, and concrete details over generalities.
3. **Be human.** Write for smart people, not robots. Avoid corporate jargon.
4. **Be confident.** State facts and capabilities clearly. Don't hedge unnecessarily.
5. **Be honest.** Never overstate capabilities. Stealth mode earns trust through restraint.

### Tone by Context

| Context            | Tone                                                      |
|--------------------|-----------------------------------------------------------|
| Hero / Headlines   | Bold, concise, aspirational but grounded                   |
| Product Features   | Clear, technical but accessible, outcome-focused           |
| About / Company    | Warm, mission-driven, authentic                            |
| Legal Pages        | Formal, precise, unambiguous                               |
| Blog / Resources   | Thoughtful, educational, opinionated where appropriate      |
| Error Messages     | Helpful, calm, actionable                                  |
| CTAs               | Action-oriented, specific ("Get early access", not "Learn more") |

### Language Rules

- Use active voice: "Our agents process 1M+ interactions" not "1M+ interactions are processed"
- Use second person for user-facing copy: "You deploy. You measure. You scale."
- Sentence case for headings (not Title Case, except for proper nouns)
- No exclamation marks in headlines or body copy
- Oxford comma: always
- Numbers: use digits for 1+, spell out "zero" and "one" in prose

---

## 3. Trademark & Logo Usage

### Logo Rules

| Rule                             | Requirement                                      |
|----------------------------------|--------------------------------------------------|
| Clear space                      | Minimum padding = height of the logo mark on all sides |
| Minimum size                     | 24px height for digital, 12mm for print          |
| Background                       | Use on dark backgrounds (preferred) or light with approved variant |
| Modifications                    | Never stretch, rotate, recolor, add effects, or alter the logo |
| Co-branding                      | Logo must always be equal or larger than partner logos |
| Favicon                          | Use the logomark only (no wordmark) at 32x32px and 16x16px |

### Logo Variants

| Variant            | Usage                                |
|--------------------|--------------------------------------|
| Full (mark + wordmark) | Primary — website header, hero, docs |
| Logomark only      | Favicon, app icon, social avatars     |
| Monochrome white   | On dark backgrounds                   |
| Monochrome dark    | On light backgrounds (if light theme) |

### Logo Don'ts

- Do not place the logo on busy/low-contrast backgrounds
- Do not use the logo as a pattern or texture
- Do not add drop shadows, outlines, or gradients to the logo
- Do not animate the logo without brand team approval
- Do not combine the logo with other symbols or text

---

## 4. Trademark Notices

### On the Website

- First mention of the company name on each page should include the
  appropriate trademark symbol: `™` (unregistered) or `®` (registered)
- Footer must include: `© {YEAR} {Company Name}. All rights reserved.`
- Third-party trademarks mentioned on the site must include attribution:
  `"{Name}" is a trademark of {Owner}.`

### In Legal Pages

The following pages are **required**:

| Page              | URL Path          | Content                                       |
|-------------------|-------------------|-----------------------------------------------|
| Terms of Service  | `/terms`          | Usage terms, liability, governing law          |
| Privacy Policy    | `/privacy`        | Data collection, cookies, GDPR/CCPA compliance |
| Cookie Policy     | `/cookies`        | Cookie types, consent, opt-out                 |
| Trademark Policy  | `/trademark`      | Logo usage, naming guidelines, permissions     |

### Trademark Usage in Copy

- Always capitalize the company name as registered
- Never use the company name as a verb or generic noun
- Never pluralize or possessivize the trademark incorrectly

**Correct:** "Deploy agents with {Company Name}."
**Incorrect:** "{company-name} your workflows." (used as verb)

---

## 5. Brand Assets & File Structure

### Recommended Asset Directory

```
/public
  /brand
    /logos
      logo-full-white.svg
      logo-full-dark.svg
      logo-mark-white.svg
      logo-mark-dark.svg
      favicon.ico
      favicon-32x32.png
      favicon-16x16.png
      apple-touch-icon.png
    /og-images
      og-default.png        (1200x630)
      og-product.png
      og-blog.png
    /fonts
      inter-var.woff2
      jetbrains-mono.woff2
```

### Open Graph Defaults

```html
<meta property="og:type" content="website" />
<meta property="og:title" content="{Page Title} — {Company Name}" />
<meta property="og:description" content="{150-char description}" />
<meta property="og:image" content="/brand/og-images/og-default.png" />
<meta property="og:url" content="https://{domain}/{path}" />
<meta name="twitter:card" content="summary_large_image" />
```

---

## 6. Legal & Compliance Checklist

Before launch, ensure:

- [ ] Privacy Policy published and linked in footer
- [ ] Terms of Service published and linked in footer
- [ ] Cookie consent banner implemented (GDPR/CCPA compliant)
- [ ] Cookie Policy published
- [ ] `©` copyright notice in footer with current year
- [ ] Trademark symbols on first use per page
- [ ] Third-party trademark attributions included
- [ ] Accessibility audit passed (WCAG 2.1 AA)
- [ ] `robots.txt` and `sitemap.xml` deployed
- [ ] SSL/TLS certificate active (HTTPS only)
- [ ] GDPR data subject request mechanism available (if applicable)
- [ ] CCPA "Do Not Sell" link (if applicable to your audience)
- [ ] All contact/form submissions have consent checkboxes
- [ ] Email collection includes opt-in confirmation (double opt-in preferred)

---

## 7. Stealth Mode–Specific Guidelines

Since the company is in stealth mode, additional rules apply:

### What to Reveal

- Company name and logo
- High-level value proposition (1–2 sentences)
- Problem space you operate in
- Social proof (investor names, client count, metrics) — if approved
- Waitlist / early access CTA
- Hiring page (if actively recruiting)
- LinkedIn and Twitter/X links

### What NOT to Reveal

- Detailed product architecture or technical implementation
- Proprietary algorithms or model details
- Specific customer names without written permission
- Roadmap or unreleased feature details
- Pricing (until publicly launched)
- Internal metrics not approved for public sharing

### Messaging Framework for Stealth

```
Layer 1 (Public):    What problem we solve + high-level how
Layer 2 (Gated):     Product details behind waitlist / NDA
Layer 3 (Internal):  Technical architecture, roadmap, financials
```

Only Layer 1 content appears on the public website.

---

## 8. Content Review & Approval

| Content Type         | Requires Approval From        |
|----------------------|-------------------------------|
| Homepage copy        | Founder / Head of Marketing   |
| Legal pages          | Legal counsel                 |
| Customer logos/names | Customer + internal approval  |
| Blog posts           | Content lead                  |
| Press mentions       | Founder                       |
| Brand asset changes  | Brand/Design lead             |

---

## 9. Developer Handoff Notes

- Use CSS custom properties (variables) for all colors, spacing, and typography
- Implement dark theme as default; light theme as optional toggle
- Use a component library approach (e.g., Tailwind + Headless UI, or Radix)
- All copy should be externalized (CMS or JSON) — not hardcoded in components
- Images served via CDN with responsive `srcset`
- Analytics: defer loading, respect Do Not Track
- Forms: validate client-side AND server-side, sanitize all inputs
- No third-party scripts without privacy review
