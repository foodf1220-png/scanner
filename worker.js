/**
 * worker.js - Cloudflare Worker: 장전 급등주 알림 + 실시간 급등주 신호 (GitHub Actions) 자동 실행
 *
 * [장전] 한국 22:00 (월~금, cron "0 13 * * 1-5" = UTC 13:00) -> premarket.yml 실행
 *        프리마켓 +20% 이상 급등주를 텔레그램으로 (한국은 서머타임이 없어 1년 내내 22:00)
 *
 * [실시간] 아래
 * 매일(월~금) 미국 장 시작 5분 전에 GitHub Actions 를 켜면, Actions 안에서 scanner.py --live 가
 * 장 시작부터 1분마다 확인하고 신호가 나는 즉시 텔레그램으로 보냄 (뉴욕 11:30 신호 시간 끝나면 종료)
 *
 *   1차  뉴욕 09:25 (한국 23:25, 서머타임 22:25)  -> 무조건 workflow_dispatch 실행
 *   2차  뉴욕 09:35 (한국 23:35, 서머타임 22:35)  -> 백업: 오늘 실행 중이거나 성공한 기록이 없을 때만 실행
 *        확인 과정 자체가 실패하면 안전하게 그냥 실행
 *   주말(뉴욕)과 일요일(한국)은 건너뜀. 미국 휴장일은 scanner.py 가 알아서 안내 후 종료
 *
 * Cloudflare cron 은 UTC 고정이라 서머타임(UTC 13시)과 표준시(UTC 14시)를 둘 다 걸어 두고,
 * 코드에서 뉴욕 현지 시각이 09:25 / 09:35 인 쪽만 실행함 (서머타임 자동 반영)
 *   cron: "25,35 13,14 * * 1-5"
 *
 * GitHub 토큰은 코드에 쓰지 않음 -> Cloudflare 비밀값(Secret) GH_TOKEN
 */
const REPO = "foodf1220-png/scanner";
const WORKFLOW = "live.yml";
const PREMARKET_WORKFLOW = "premarket.yml";
const PREMARKET_CRON = "0 13 * * 1-5";   // 한국 22:00 장전 급등주
const MAIN_NY = "09:25";
const BACKUP_NY = "09:35";

function ghHeaders(token) {
  return {
    Authorization: `token ${token}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "Scanner-Worker",
  };
}

// 뉴욕 현지 요일·시각 ("Mon", "09:25")
function nyTime(date) {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York", hourCycle: "h23", weekday: "short", hour: "2-digit", minute: "2-digit",
    }).formatToParts(date).map((x) => [x.type, x.value])
  );
  return { weekday: p.weekday, hm: `${p.hour}:${p.minute}` };
}

// 오늘 이미 실행 중(대기 포함)이거나 성공한 실행이 있는지. 조회 실패면 에러를 던짐
async function alreadyRunningOrDone(token, date) {
  const today = date.toISOString().slice(0, 10); // 1차·2차 모두 같은 UTC 날짜
  const url =
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/runs` +
    `?per_page=20&created=${encodeURIComponent(">=" + today)}`;
  const res = await fetch(url, { headers: ghHeaders(token) });
  if (!res.ok) throw new Error(`실행 이력 조회 실패 (HTTP ${res.status})`);
  const runs = (await res.json()).workflow_runs || [];
  console.log(`오늘(${today}) 실행 ${runs.length}건:`,
    runs.map((r) => `${r.created_at} ${r.status}/${r.conclusion}`).join(", ") || "없음");
  return runs.some(
    (r) => r.created_at.startsWith(today) &&
      (r.status !== "completed" || r.conclusion === "success") // 대기·실행 중이거나 성공
  );
}

async function dispatch(token, label, workflow = WORKFLOW) {
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: { ...ghHeaders(token), "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "main" }),
    }
  );
  if (res.status === 204) {
    console.log(`[${label}] GitHub Actions ${workflow} 실행 요청 성공`);
    return;
  }
  const body = await res.text();
  console.log(`[${label}] 실행 요청 실패 HTTP ${res.status}: ${body.slice(0, 300)}`);
  throw new Error(`workflow_dispatch 실패 (HTTP ${res.status})`);
}

export default {
  async scheduled(event, env, ctx) {
    const when = new Date(event.scheduledTime || Date.now());
    const ny = nyTime(when);
    const kstDay = new Date(when.getTime() + 9 * 3600 * 1000).getUTCDay(); // 0 = 일요일(한국)
    if (ny.weekday === "Sat" || ny.weekday === "Sun" || kstDay === 0) {
      console.log(`주말 — 건너뜀 (뉴욕 ${ny.weekday} ${ny.hm})`);
      return;
    }

    const token = env.GH_TOKEN;
    if (!token) {
      throw new Error("GH_TOKEN 비밀값이 없습니다 — Settings > Variables and Secrets 에서 등록하세요");
    }

    if (event.cron === PREMARKET_CRON) {         // 한국 22:00 장전 급등주
      await dispatch(token, "장전 급등주", PREMARKET_WORKFLOW);
      return;
    }
    if (ny.hm !== MAIN_NY && ny.hm !== BACKUP_NY) {
      console.log(`뉴욕 ${ny.hm} — 서머타임 반대쪽 시각이라 건너뜀`);
      return;
    }

    if (ny.hm === BACKUP_NY) {
      try {
        if (await alreadyRunningOrDone(token, when)) {
          console.log("[2차 백업] 오늘 실행 중이거나 성공한 기록 있음 — 건너뜀");
          return;
        }
        console.log("[2차 백업] 오늘 실행 기록 없음 — 실행");
      } catch (e) {
        console.log("[2차 백업] 확인 실패 — 안전하게 실행:", e.message || e);
      }
      await dispatch(token, "2차 백업");
      return;
    }

    await dispatch(token, "1차");
  },

  async fetch() {
    return new Response(
      "scanner Worker (cron 전용)\n장전 급등주: 한국 22:00\n실시간 신호 1차: 뉴욕 09:25 (한국 23:25 / 서머타임 22:25)\n" +
        "실시간 신호 2차: 뉴욕 09:35 실행 기록 없으면 실행\n주말 제외",
      { headers: { "content-type": "text/plain; charset=utf-8" } }
    );
  },
};
