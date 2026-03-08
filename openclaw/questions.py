from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Final

from openclaw.answer_bank import expand_placeholders, is_human_sentinel, match_question_bank


logger = logging.getLogger(__name__)

STANDARD_QUESTION_PATTERNS: Final[list[tuple[re.Pattern[str], str]]] = [
    (re.compile(r"referred|referral", re.I), "No"),
    (re.compile(r"how did you hear|where did you hear|source", re.I), "Online Job Board"),
    (re.compile(r"start date|when can you start|available to start", re.I), "Summer 2026"),
    (re.compile(r"salary expectation|compensation expectation|desired salary", re.I), "Open / Negotiable"),
]


def match_standard_answer(question_text: str, *, how_heard_answer: str = "Online Job Board") -> str | None:
    text = question_text.strip()
    if not text:
        return None
    for pattern, answer in STANDARD_QUESTION_PATTERNS:
        if pattern.search(text):
            if "hear" in pattern.pattern or "source" in pattern.pattern:
                return how_heard_answer
            return answer
    return None


@dataclass(slots=True)
class QuestionAnswerer:
    region_name: str = os.getenv("OPENCLAW_BEDROCK_REGION", "us-east-1")
    # Default Bedrock model for custom Q&A / cover letters.
    # Override by passing `QuestionAnswerer(model_id=...)` if needed.
    # Use `global.` prefix for cross-Region inference profile (required for on-demand).
    model_id: str = os.getenv(
        "OPENCLAW_BEDROCK_MODEL_ID", "arn:aws:bedrock:us-east-1:128009599260:inference-profile/us.anthropic.claude-sonnet-4-6"
    )
    max_tokens: int = 260
    temperature: float = 0.2

    async def cover_letter(
        self,
        *,
        company: str,
        role: str,
        profile_summary: str,
        resume_text: str = "",
        job_description: str = "",
    ) -> str:
        """
        Generate a tailored cover letter body. Returns plain text.
        """
        import asyncio as _aio
        import functools as _ft

        letter = await _aio.get_event_loop().run_in_executor(
            None,  # default thread-pool
            _ft.partial(
                self._cover_letter_with_bedrock,
                company=company,
                role=role,
                profile_summary=profile_summary,
                resume_text=resume_text,
                job_description=job_description,
            ),
        )
        if letter:
            return letter

        # Fallback: keep it generic but still professional and truthful.
        return (
            f"I'm applying for the {role} role at {company}. I'm a UCSB Computer Science student focused on building "
            "reliable backend and full-stack systems, with experience shipping projects end-to-end and iterating quickly "
            "based on feedback.\n\n"
            "What excites me about this opportunity is the chance to contribute to production engineering work while learning "
            "from a strong team. I enjoy taking ownership of ambiguous problems, translating requirements into milestones, "
            "and delivering robust implementations with clear communication.\n\n"
            "I'd love the opportunity to bring that execution mindset to the team and contribute as a high-ownership intern. "
            "Thank you for your time and consideration."
        )

    async def answer(
        self,
        question: str,
        *,
        company: str,
        role: str,
        profile_summary: str,
        resume_text: str = "",
        job_description: str = "",
        question_bank: list[tuple[str, str]] | None = None,
        template_values: dict[str, str] | None = None,
        source: str | None = None,
    ) -> str:
        template_values = dict(template_values or {})
        source_norm = (source or "").strip().lower()
        how_heard = "Online Job Board"
        if source_norm in {"simplify", "simplifyjobs"}:
            how_heard = "SimplifyJobs (GitHub)"
        template_values.setdefault("how_heard", how_heard)
        template_values.setdefault("company", company)
        template_values.setdefault("role", role)

        # Highest priority: explicit user-provided answer bank (authoritative).
        if question_bank:
            bank_answer = match_question_bank(question, question_bank)
            if bank_answer is not None:
                rendered = expand_placeholders(str(bank_answer), template_values).strip()
                if is_human_sentinel(rendered):
                    logger.debug("  Q answer: __HUMAN__ sentinel — skipping.")
                    return ""
                logger.info("  Q answer source: answer bank match")
                return rendered

        standard = match_standard_answer(question, how_heard_answer=how_heard)
        if standard:
            logger.info("  Q answer source: standard pattern match -> %s", standard[:50])
            return standard

        logger.info("  Q answer source: calling Bedrock LLM...")
        answer = self._answer_with_bedrock(
            question=question,
            company=company,
            role=role,
            profile_summary=profile_summary,
            resume_text=resume_text,
            job_description=job_description,
            answer_bank_context=_format_answer_bank_context(question_bank or [], template_values),
        )
        if answer:
            logger.info("  Q answer source: Bedrock LLM returned %d chars", len(answer))
            return answer

        logger.info("  Q answer source: fallback template")
        return self._fallback_answer(question=question, company=company, role=role)

    async def tailor_resume(
        self,
        *,
        company: str,
        role: str,
        resume_text: str,
        job_description: str = "",
    ) -> str:
        """
        Produce an ATS-friendly tailored resume text.
        Guardrail: must not introduce facts not present in the input resume text.
        """
        tailored = self._tailor_resume_with_bedrock(
            company=company,
            role=role,
            resume_text=resume_text,
            job_description=job_description,
        )
        if tailored:
            return tailored
        return (resume_text or "").strip()

    def _tailor_resume_with_bedrock(
        self,
        *,
        company: str,
        role: str,
        resume_text: str,
        job_description: str,
    ) -> str | None:
        try:
            import boto3  # type: ignore
        except Exception:
            return None

        resume_excerpt = (resume_text or "").strip()
        if len(resume_excerpt) > 12_000:
            resume_excerpt = resume_excerpt[:12_000]
        jd_excerpt = (job_description or "").strip()
        if len(jd_excerpt) > 10_000:
            jd_excerpt = jd_excerpt[:10_000]

        prompt = (
            "Rewrite the candidate resume to be tightly tailored to the job description and ATS-friendly.\n"
            "Rules:\n"
            "- Do NOT add any new facts. Do not invent employers, titles, dates, metrics, awards, or technologies.\n"
            "- You may only reorder sections, rephrase bullets, and emphasize relevant skills already present.\n"
            "- Keep it one page and keep the original section structure where possible.\n"
            "- Use plain text with clear section headers.\n"
            "- Keep existing dates as-is.\n\n"
            f"Target Company: {company}\n"
            f"Target Role: {role}\n\n"
            f"Job description (excerpt):\n{jd_excerpt or '[not provided]'}\n\n"
            f"Original resume text:\n{resume_excerpt or '[not provided]'}\n\n"
            "Return only the rewritten resume text."
        )

        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1800,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }

        try:
            client = boto3.client("bedrock-runtime", region_name=self.region_name)
            response = client.invoke_model(modelId=self.model_id, body=json.dumps(payload))
            body = response.get("body")
            if body is None:
                return None
            raw = body.read() if hasattr(body, "read") else body
            parsed = json.loads(raw)
            content = parsed.get("content") or []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text") or "").strip()
                    if text:
                        return text
        except Exception:
            return None
        return None

    def _cover_letter_with_bedrock(
        self,
        *,
        company: str,
        role: str,
        profile_summary: str,
        resume_text: str,
        job_description: str,
    ) -> str | None:
        try:
            import boto3  # type: ignore
        except Exception:
            return None

        resume_excerpt = (resume_text or "").strip()
        if len(resume_excerpt) > 8000:
            resume_excerpt = resume_excerpt[:8000]
        jd_excerpt = (job_description or "").strip()
        if len(jd_excerpt) > 8000:
            jd_excerpt = jd_excerpt[:8000]

        prompt = (
            "You are ghostwriting a cover letter that should read like the candidate "
            "wrote it themselves, not like a career advisor drafted it.\n\n"

            "## GUIDING PRINCIPLE\n\n"
            "The best cover letters balance three things equally:\n"
            "1. **Technical credibility**: The reader must see that the candidate can "
            "actually build things. Name specific technologies, architectures, and "
            "engineering decisions. Show you can do the work.\n"
            "2. **Company fit**: Show you understand what THIS company does, what problems "
            "they face, and why your specific background maps to their needs.\n"
            "3. **How you think**: Briefly reveal your engineering mindset, the tradeoffs "
            "you weigh, and the kind of problems you gravitate toward. Not as abstract "
            "philosophy, but woven into the technical stories.\n\n"
            "None of these should dominate. A letter that is all philosophy sounds fluffy. "
            "A letter that is all tech specs sounds like a resume in paragraph form. "
            "A letter that is all company flattery sounds hollow. Blend all three in "
            "every paragraph.\n\n"

            "## HARD BANS (violating any = unusable letter)\n\n"
            "- NO em dashes (--) anywhere. Use commas, periods, semicolons.\n"
            "- NO smart/curly quotes. Use straight ASCII quotes and apostrophes only.\n"
            "- NO generic adjectives or filler: passionate, driven, excited, thrilled, "
            "dynamic, innovative, cutting-edge, leverage, synergy, utilize, fast-paced, "
            "collaborative environment, make an impact, deeply resonates, aligns with my "
            "passion, I am confident that, I would love the opportunity, I am eager to, "
            "honed my skills, I believe I would be a great fit, strong foundation.\n"
            "- NO template openers: 'As a...', 'With my experience in...', "
            "'I am writing to express...', 'Throughout my career...', 'I look forward to...'.\n"
            "- NO standalone brag lines. If you mention a metric (e.g., 90% accuracy), "
            "it must be in service of explaining a decision or outcome, not dropped to impress.\n"
            "- NO out-of-context name-dropping. Every project/company mentioned must connect "
            "to a specific need from this company's JD. If the connection is not obvious in "
            "the same sentence or the next, do not include it.\n"
            "- NO purely philosophical sentences with zero technical content. Every sentence "
            "that discusses mindset or values must also contain a concrete technical detail.\n\n"

            "## WRITING PROCESS\n\n"
            "1. **Understand the company and role technically**:\n"
            "   - What does this company build? What is the product, the stack, the domain?\n"
            "   - What specific engineering problems does this role involve?\n"
            "   - What technical skills does the JD ask for (use their exact words)?\n"
            "   - Using your training data, recall any recent company news, product launches, "
            "open-source work, blog posts, or events. Pick the most relevant 1-2 items.\n\n"
            "2. **Find where the candidate's technical experience overlaps with the role**:\n"
            "   - Which 1-2 projects from the resume involve the SAME types of problems "
            "this role involves? (e.g., if the role is about deployment tooling, pick a "
            "project where the candidate dealt with deployment.)\n"
            "   - For each project, identify: the technical problem, the approach taken, "
            "what technologies were used, and the outcome or lesson.\n\n"
            "3. **Write each anecdote as: problem -> technical approach -> insight**:\n"
            "   - Start with the problem (1 sentence).\n"
            "   - Describe the technical approach briefly, naming real tools/frameworks "
            "(1-2 sentences).\n"
            "   - End with what happened or what the candidate learned, connecting it to "
            "the company's needs (1 sentence).\n"
            "   - Example of the right balance:\n"
            "     'At Ryft, our commission engine needed to go from an internal script to "
            "something insurance sales teams would trust with real money. I built the "
            "calculation layer in Python using Decimal for financial precision and added "
            "an audit trail that logged every step, then rewrote the Next.js frontend "
            "three times based on user testing until ops teams stopped calling us to "
            "verify numbers. That process of iterating between backend correctness and "
            "frontend clarity is exactly what preparing a labeling tool for external "
            "users requires.'\n"
            "   - Notice: specific tech (Python Decimal, Next.js), a real decision "
            "(audit trail, frontend rewrites), an outcome (ops teams trusted it), AND "
            "a connection to the company. All in three sentences.\n\n"
            "4. **Weave in company research naturally**:\n"
            "   - Reference a specific product, event, or technical challenge from the company.\n"
            "   - Frame it as something that caught the candidate's attention because of "
            "their own experience, not as generic praise.\n\n"

            "## LETTER STRUCTURE (4 paragraphs, 250-350 words)\n\n"
            "**Opening (2-3 sentences):** Start with something specific about the company "
            "or role (a product, a technical challenge from the JD, a recent event). "
            "Connect it to the candidate's experience. Establish why this is a natural fit.\n\n"
            "**Body 1 (3-4 sentences):** Most relevant technical experience. Tell it as "
            "problem -> approach -> result, with specific tech named. End by connecting it "
            "to a requirement from the JD.\n\n"
            "**Body 2 (3-4 sentences):** A second experience that shows different range "
            "(e.g., if body 1 was backend, body 2 could be frontend or deployment or "
            "a hackathon project). Same structure: problem -> approach -> result -> "
            "connection to company.\n\n"
            "**Closing (2-3 sentences):** Name a specific product area or technical "
            "challenge at the company the candidate wants to work on. Briefly say why, "
            "grounded in what was described above. End warmly but not with a cliche.\n\n"

            "## FORMAT\n"
            "- Start with 'Dear Hiring Manager,' on its own line.\n"
            "- No addresses, no dates.\n"
            "- End with 'Sincerely,' then the candidate's full name on the next line.\n"
            "- No 'Enclosure' or 'Attachment' lines.\n"
            "- ASCII characters only. Straight quotes, regular hyphens.\n\n"

            "## FINAL SELF-CHECK\n"
            "Re-read the letter and ask:\n"
            "1. Does every paragraph contain at least one named technology and one "
            "connection to the company? If not, add them.\n"
            "2. Could this letter work for another company if you swap the name? "
            "If yes, it is too generic. Rewrite.\n"
            "3. Does it sound like a real engineer talking about their work, or does it "
            "sound like a career counselor wrote it? If the latter, make it more concrete.\n"
            "4. Is there a good balance of technical detail, company knowledge, and "
            "personal engineering judgment? If any one dominates, rebalance.\n"
            "5. Are there any em dashes, smart quotes, or banned phrases? Remove them.\n\n"

            f"## Company & Role\n"
            f"Company: {company}\n"
            f"Role: {role}\n\n"
            f"## Job Description\n{jd_excerpt or '[not provided]'}\n\n"
            f"## Candidate Profile\n{profile_summary}\n\n"
            f"## Candidate Resume\n{resume_excerpt or '[not provided]'}\n\n"
            "Write the cover letter now. Return ONLY the letter text, nothing else."
        )

        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1500,
            "temperature": 0.45,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }

        try:
            client = boto3.client("bedrock-runtime", region_name=self.region_name)
            response = client.invoke_model(modelId=self.model_id, body=json.dumps(payload))
            body = response.get("body")
            if body is None:
                return None
            raw = body.read() if hasattr(body, "read") else body
            parsed = json.loads(raw)
            content = parsed.get("content") or []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text") or "").strip()
                    if text:
                        return text
        except Exception:
            return None
        return None

    def _answer_with_bedrock(
        self,
        *,
        question: str,
        company: str,
        role: str,
        profile_summary: str,
        resume_text: str,
        job_description: str,
        answer_bank_context: str = "",
    ) -> str | None:
        try:
            import boto3  # type: ignore
        except Exception:
            logger.debug("boto3 is unavailable; using fallback for custom question.")
            return None

        resume_excerpt = (resume_text or "").strip()
        if len(resume_excerpt) > 8000:
            resume_excerpt = resume_excerpt[:8000]
        jd_excerpt = (job_description or "").strip()
        if len(jd_excerpt) > 8000:
            jd_excerpt = jd_excerpt[:8000]

        prompt = (
            "You are writing concise, truthful internship application answers.\n"
            "Rules:\n"
            "- Be specific to the company and role.\n"
            "- Use only facts that appear in the provided candidate profile/resume text OR the candidate answer bank.\n"
            "- Do not invent employers, titles, dates, metrics, or technologies not present.\n"
            "- Match requested length naturally.\n"
            "- If the field seems like a short answer, write 2-3 sentences.\n"
            "- If the field is essay-style, write one paragraph.\n"
            "- Keep tone confident, professional, and direct.\n\n"
            f"Company: {company}\n"
            f"Role: {role}\n\n"
            f"Job description (excerpt):\n{jd_excerpt or '[not provided]'}\n\n"
            f"Candidate profile summary:\n{profile_summary}\n\n"
            f"Candidate resume text (excerpt):\n{resume_excerpt or '[not provided]'}\n\n"
            f"Candidate answer bank (authoritative; excerpt):\n{answer_bank_context or '[not provided]'}\n\n"
            f"Question: {question}\n\n"
            "Return only the final answer text."
        )

        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }

        try:
            client = boto3.client("bedrock-runtime", region_name=self.region_name)
            response = client.invoke_model(modelId=self.model_id, body=json.dumps(payload))
            body = response.get("body")
            if body is None:
                return None
            raw = body.read() if hasattr(body, "read") else body
            parsed = json.loads(raw)
            content = parsed.get("content") or []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text") or "").strip()
                    if text:
                        return text
        except Exception as exc:
            logger.debug("Bedrock answer generation failed: %s", exc)
            return None
        return None

    def pick_option(
        self,
        *,
        question_label: str,
        intended_answer: str,
        options: list[str],
    ) -> int | None:
        """
        LLM fallback: given a question, the intended answer, and a list of
        dropdown options, return the 0-based index of the best option.
        Used when fuzzy matching fails to find a match.
        """
        if not options:
            return None

        try:
            import boto3  # type: ignore
        except Exception:
            logger.debug("boto3 unavailable; cannot LLM-pick option.")
            return None

        numbered = "\n".join(f"  {i}: {opt}" for i, opt in enumerate(options))
        prompt = (
            "You are helping fill out a job application form.\n\n"
            f"Question / field label: {question_label}\n\n"
            f"The applicant's intended answer: {intended_answer}\n\n"
            f"Available dropdown options:\n{numbered}\n\n"
            "Which option number best matches the applicant's intended answer?\n"
            "Reply with ONLY the option number (e.g. '0' or '2'). Nothing else."
        )

        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 10,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }

        try:
            client = boto3.client("bedrock-runtime", region_name=self.region_name)
            response = client.invoke_model(modelId=self.model_id, body=json.dumps(payload))
            body = response.get("body")
            if body is None:
                return None
            raw = body.read() if hasattr(body, "read") else body
            parsed = json.loads(raw)
            content = parsed.get("content") or []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text") or "").strip()
                    # Extract the number
                    match = re.search(r"\d+", text)
                    if match:
                        idx = int(match.group())
                        if 0 <= idx < len(options):
                            logger.info("  LLM option picker: picked #%d '%s' for '%s'",
                                        idx, options[idx][:40], intended_answer[:30])
                            return idx
        except Exception as exc:
            logger.debug("LLM option picker failed: %s", exc)
            return None
        return None

    def _fallback_answer(self, *, question: str, company: str, role: str) -> str:
        text = question.lower()
        if "why" in text and "company" in text:
            return (
                f"I'm excited about {company} because of the quality and impact of its products, and the chance "
                f"to learn from strong engineering teams. This {role} role aligns with my goal of building reliable "
                "software at scale while contributing quickly in a collaborative environment."
            )
        if "challenge" in text or "difficult" in text:
            return (
                "A challenging project I handled required breaking an ambiguous problem into milestones, validating "
                "assumptions early, and iterating based on feedback. That process improved both delivery speed and quality."
            )
        if "strength" in text:
            return (
                "My strengths are structured problem-solving, clear communication, and ownership from debugging through delivery. "
                "I focus on shipping reliable solutions and documenting decisions so teams can move faster."
            )
        return (
            f"I’m interested in this {role} opportunity at {company} because it combines meaningful product impact with "
            "a strong environment for growth. I can contribute through practical software engineering execution, fast learning, "
            "and consistent collaboration."
        )


def _format_answer_bank_context(
    bank: list[tuple[str, str]], template_values: dict[str, str], *, limit_chars: int = 2400
) -> str:
    lines: list[str] = []
    for pattern, answer in bank:
        pat = str(pattern or "").strip()
        if not pat:
            continue
        rendered = expand_placeholders(str(answer or ""), template_values).strip()
        if not rendered or is_human_sentinel(rendered):
            continue
        lines.append(f"- {pat}: {rendered}")
        if sum(len(l) + 1 for l in lines) >= limit_chars:
            break

    out = "\n".join(lines).strip()
    if len(out) > limit_chars:
        out = out[:limit_chars].rsplit("\n", 1)[0].strip()
    return out
