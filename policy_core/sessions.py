"""Sessions operations for one policy engine instance."""
import sqlite3
import time
import re

FOLLOWUP_HEAD = re.compile(r'^\s*(?:那|那么|还有|另外|它|其|该|此|这个|这些|上述|上面|刚才|接着|继续|再|其中|同样|对比|比一比)')


class SessionsMixin:
    def resolve_followup(self, session_id, question):
        """把"那它的目标呢"这类追问，补全成可独立检索的问题。

    做法是把上一轮的问题作为主题上下文接上，并补回上一轮明确点过的地名；
    不继承上一轮"检索到哪篇就取哪篇"的规划期，否则追问会被过度收窄。
    """
        if not session_id: return question, None
        with self.SESSION_LOCK:
            ctx=self.SESSIONS.get(session_id)
        if not ctx: return question, None
        if len(question)>24 and not self.FOLLOWUP_HEAD.search(question): return question, None
        parts=[]
        area=ctx.get('area') or ''
        if area and self.normalized_text(area) not in self.normalized_text(question):
            parts.append(area)
        previous=ctx.get('question') or ''
        if previous and self.normalized_text(previous)!=self.normalized_text(question):
            parts.append(previous)
        parts.append(question)
        enriched=' '.join(parts).strip()
        if len(enriched)>160: enriched=enriched[:160]
        return enriched, ctx


    def remember_session(self, session_id, question, rows):
        """记下本轮问题里能识别出的地区，供下一轮追问继承。"""
        if not session_id: return
        area=''
        # 只认问题里明说的地名，不拿检索结果的元数据反推范围。
        try:
            meta=self._metadata_scope(self.db())
            norm=self.normalized_text(question)
            named=[a for a in meta.get('areas',{}) if len(a)>=3 and self.normalized_text(a) in norm]
            if named: area=max(named,key=len)
            if not area and rows:
                area=rows[0].get('area_name') or ''
        except sqlite3.DatabaseError:
            pass
        with self.SESSION_LOCK:
            if len(self.SESSIONS)>=self.MAX_SESSIONS: self.SESSIONS.pop(next(iter(self.SESSIONS)))
            self.SESSIONS[session_id]={'question':question,'area':area,'ts':time.time()}


