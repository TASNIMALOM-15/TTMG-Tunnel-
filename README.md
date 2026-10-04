# Telegram Cloud Hub Bot — সেটআপ গাইড (বাংলা)

Google Drive ⇄ Telegram ⇄ TeraBox ⇄ MEGA — এক বটে।

## ১. ফাইল স্ট্রাকচার

```
telegram-cloud-hub/
├── bot.py             # Pyrogram বট, বাটন হ্যান্ডলার, কিউ/ওয়ার্কার, ওয়েব সার্ভার
├── config.py          # Environment variable লোডার
├── utils.py           # নাম/প্রগ্রেস/ডাউনলোড/ffmpeg হেল্পার
├── rclone_utils.py    # rclone র‍্যাপার (rcat স্ট্রিমিং, copyto, copyurl)
├── gdrive.py          # Google Drive হেল্পার
├── mega.py            # MEGA হেল্পার (rclone + megatools)
├── terabox.py         # TeraBox হেল্পার (কুকি-ভিত্তিক, অনানুষ্ঠানিক)
├── start.sh           # env থেকে rclone.conf বানিয়ে বট চালু করে
├── requirements.txt
├── Dockerfile         # Python 3.11 + FFmpeg + rclone + megatools
├── render.yaml        # (ঐচ্ছিক) Render Blueprint
├── .env.example
├── .gitignore
└── .dockerignore
```

সব ফাইল রুট ফোল্ডারে — তাই মোবাইল থেকে আপলোড করা সহজ।

## ২. কীভাবে কাজ করে (ট্রান্সফার রুট)

| সোর্স → গন্তব্য | পদ্ধতি | ডিস্ক লাগে? |
|---|---|---|
| Telegram → Drive | `stream_media` → `rclone rcat` (পাইপ) | না (সত্যিকারের স্ট্রিম) |
| Telegram → MEGA | পাইপ, কিন্তু rclone MEGA-র জন্য টেম্প ডিস্কে স্পুল করে | হ্যাঁ (টেম্প) |
| Telegram → TeraBox | ডিস্কে নামিয়ে চাঙ্ক আপলোড | হ্যাঁ |
| Drive → MEGA | `rclone backend copyid` (সরাসরি), ব্যর্থ হলে ডিস্ক-স্টেজিং | সাধারণত না |
| Link → Drive/MEGA | `rclone copyurl` | না |
| MEGA/TeraBox/Drive → Telegram বা অন্য | ডিস্কে নামিয়ে আপলোড, শেষে মুছে ফেলা | হ্যাঁ |

RAM কম রাখতে: ১টি ওয়ার্কার, ৮MB বাফার, ১MB চাঙ্ক। ডিস্কে স্টেজ করা ফাইল কাজ শেষে/ব্যর্থ হলে মুছে যায়।

## ৩. GitHub-এ আপলোড (মোবাইল)

1. ব্রাউজারে github.com খুলুন → লগইন → ব্রাউজারের **Desktop site** অন করুন।
2. **New repository** → নাম দিন (যেমন `cloud-hub-bot`) → **Private** → Create।
3. **uploading an existing file** লিংকে চাপুন → ফোনের Files থেকে সব ফাইল সিলেক্ট করে আপলোড → **Commit changes**।
4. `.env.example`, `.gitignore`, `.dockerignore` মোবাইলে হিডেন ফাইল হিসেবে সিলেক্ট নাও হতে পারে। তখন **Add file → Create new file** দিয়ে নাম লিখে কনটেন্ট পেস্ট করুন।
5. **কখনো আসল `.env` বা পাসওয়ার্ড/টোকেন GitHub-এ আপলোড করবেন না।**
6. ফাইল এডিট করলে Windows লাইন-এন্ডিং (CRLF) এড়ান, নাহলে `start.sh` চলবে না।

## ৪. ক্রেডেনশিয়াল সংগ্রহ

### Telegram
- **API_ID / API_HASH**: https://my.telegram.org → API development tools → অ্যাপ তৈরি।
- **BOT_TOKEN**: Telegram-এ @BotFather → `/newbot`।
- **OWNER_IDS**: @userinfobot-কে মেসেজ দিন, আপনার সংখ্যার ID পাবেন। একাধিক হলে কমা দিয়ে লিখুন।

### Google Drive (OAuth টোকেন — সুপারিশকৃত)
1. PC বা Android-এর Termux-এ rclone ইনস্টল করুন (`pkg install rclone`)।
2. (সুপারিশকৃত) https://console.cloud.google.com → নতুন প্রজেক্ট → **Google Drive API** Enable → **OAuth consent screen** (External) → **Publish app / In production** করুন → **Credentials → OAuth client ID (Desktop app)** → Client ID ও Secret নিন।
   > ⚠️ অ্যাপ "Testing" মোডে থাকলে টোকেন ৭ দিনে মেয়াদ শেষ হয়ে যায়। অবশ্যই **In production** করুন।
3. চালান: `rclone authorize "drive" "CLIENT_ID" "CLIENT_SECRET"` (নিজের ক্লায়েন্ট না থাকলে শুধু `rclone authorize "drive"`)।
4. ব্রাউজারে অনুমতি দিন। টার্মিনালে `{...}` JSON টোকেন আসবে — এটি **এক লাইনে** কপি করুন → `GDRIVE_TOKEN`।
5. নিজের ক্লায়েন্ট ব্যবহার করলে `GDRIVE_CLIENT_ID` ও `GDRIVE_CLIENT_SECRET`-ও সেট করুন।
6. **Service Account (বিকল্প)**: Service Account-এর নিজস্ব স্টোরেজ কোটা নেই। শুধু **Shared Drive** (সাধারণত Google Workspace) থাকলে কাজ করে। JSON ফাইলকে `base64 -w0 sa.json` দিয়ে এনকোড করে `GDRIVE_SA_B64`-তে দিন এবং `GDRIVE_TEAM_DRIVE_ID` সেট করুন। সাধারণ ফ্রি Gmail-এর জন্য OAuth টোকেন ব্যবহার করুন।

Drive লিংক থেকে ডাউনলোডের জন্য ফাইলটি আপনার অ্যাকাউন্টের কাছে অ্যাক্সেসযোগ্য হতে হবে (যেমন "Anyone with the link")।

### MEGA
- `MEGA_EMAIL` ও `MEGA_PASSWORD` দিন। **2FA চালু থাকলে rclone লগইন করতে পারবে না** — বন্ধ রাখতে হবে (বা আলাদা অ্যাকাউন্ট ব্যবহার করুন)।
- MEGA পাবলিক লিংক (ফাইল/ফোল্ডার) থেকে ডাউনলোড `megatools` দিয়ে হয়। ফ্রি অ্যাকাউন্টে ডাউনলোড ব্যান্ডউইথ কোটা সীমিত।
- `MEGA_LOGIN_FOR_DOWNLOAD=true` দিলে ডাউনলোডেও আপনার লগইন ব্যবহার হবে।

### TeraBox (কুকি)
TeraBox-এর কোনো অফিসিয়াল API নেই। বট ওয়েবসাইটের এন্ডপয়েন্ট ও আপনার কুকি ব্যবহার করে।
1. ফোনে Firefox + **Cookie-Editor** এক্সটেনশন (বা PC-তে Chrome) দিয়ে terabox.com-এ লগইন করুন।
2. কুকি লিস্ট থেকে **`ndus`** এর ভ্যালু কপি করুন (PC: F12 → Application → Cookies)।
3. `TERABOX_COOKIE`-এ শুধু সেই ভ্যালু (অথবা পুরো কুকি স্ট্রিং) বসান।
4. কুকি মেয়াদ শেষ হলে (বটে "কুকি মেয়াদোত্তীর্ণ" মেসেজ আসবে) নতুন করে নিন।

> ⚠️ TeraBox অংশটি অনানুষ্ঠানিক এন্ডপয়েন্টের উপর নির্ভরশীল। TeraBox পরিবর্তন করলে এটি ভেঙে যেতে পারে। প্রথমে একটি ছোট ফাইল দিয়ে টেস্ট করুন।

## ৫. Render-এ ডিপ্লয়

1. https://render.com → GitHub দিয়ে সাইন আপ।
2. **New → Web Service** → আপনার প্রাইভেট রিপো সংযুক্ত করুন।
3. **Language/Runtime: Docker** (Dockerfile অটো ধরবে)। **Build Command ও Start Command ফাঁকা রাখুন** — Dockerfile-এর `CMD` ("sh /app/start.sh") চালাবে।
4. **Instance Type: Free**।
5. **Advanced → Health Check Path**: `/health`।
6. **Environment Variables** যোগ করুন:

| Key | কী দেবেন |
|---|---|
| `API_ID`, `API_HASH`, `BOT_TOKEN` | Telegram থেকে |
| `OWNER_IDS` | আপনার ID (কমা দিয়ে একাধিক) |
| `GDRIVE_TOKEN` | rclone authorize-এর এক-লাইন JSON |
| `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET` | নিজের OAuth ক্লায়েন্ট ব্যবহার করলে |
| `MEGA_EMAIL`, `MEGA_PASSWORD` | MEGA লগইন |
| `TERABOX_COOKIE` | `ndus` ভ্যালু |
| `WORKERS` | `1` (ফ্রি টিয়ারের জন্য) |
| `GDRIVE_FOLDER`, `MEGA_FOLDER`, `TERABOX_FOLDER` | গন্তব্য ফোল্ডার, ডিফল্ট `TG-Hub` |

`PORT` ও `RENDER_EXTERNAL_URL` Render নিজেই সেট করে।

7. **Create Web Service** → লগে `bot started as @yourbot` দেখলে সফল।
8. Telegram-এ বটকে `/start` দিন।

### Keep-alive
- বট নিজেই প্রতি ১০ মিনিটে নিজের `/health` URL-এ পিং করে (`RENDER_EXTERNAL_URL` দিয়ে)।
- বাড়তি নিরাপত্তার জন্য https://uptimerobot.com-এ HTTP monitor বানান: URL `https://আপনার-অ্যাপ.onrender.com/health`, ইন্টারভাল ৫ মিনিট।

### ফ্রি টিয়ারের সীমাবদ্ধতা (সৎ কথা)
- RAM প্রায় ৫১২MB, ডিস্ক অস্থায়ী (রিস্টার্টে মুছে যায়) এবং আকার নির্দিষ্টভাবে গ্যারান্টিড নয়। ডিস্ক-স্টেজিং লাগে এমন রুটে ২GB ফাইল ডিস্কে না কুলালে বট "ডিস্কে জায়গা কম" জানাবে।
- রিস্টার্ট/রি-ডিপ্লয়ে কিউ ডাটাবেস (`/tmp`) মুছে যেতে পারে; অসমাপ্ত কাজ আবার দিতে হবে।
- Telegram বট ফাইল সর্বোচ্চ ~২GB পাঠাতে/নামাতে পারে।

## ৬. ব্যবহার

- ভিডিও/ডকুমেন্ট ফরোয়ার্ড করুন, অথবা Drive/MEGA/TeraBox/ডাইরেক্ট লিংক পাঠান → বাটন থেকে গন্তব্য বাছুন।
- একসাথে অনেক: `/bulk` → সব ফাইল/লিংক পাঠান → `/done` → গন্তব্য বাছুন।
- `/queue` কিউ দেখায়, `/cancel` চলমান কাজ থামায়, `/clear` অপেক্ষমাণ কাজ মোছে।
- শুধু `OWNER_IDS`-এর ব্যবহারকারীরা বট ব্যবহার করতে পারবেন; অন্যদের মেসেজ উপেক্ষা করা হয়।

## ৭. ট্রাবলশুটিং চেকলিস্ট

- [ ] লগে `Missing required environment variable` → ওই Env Var Render-এ যোগ করুন।
- [ ] বট সাড়া দেয় না → `OWNER_IDS`-এ আপনার সঠিক ID আছে? লগে `bot started` আছে? `BOT_TOKEN` ঠিক?
- [ ] Drive: `couldn't find root directory`/`invalid_grant` → `GDRIVE_TOKEN` এক লাইনে আছে? OAuth অ্যাপ "In production"? টোকেন নতুন করে বানান।
- [ ] Drive: `storageQuotaExceeded` → Service Account ব্যবহার করছেন। OAuth টোকেনে যান বা Shared Drive দিন।
- [ ] Drive লিংক থেকে ফাইল পাওয়া যায় না → শেয়ারিং "Anyone with the link" করুন।
- [ ] MEGA login failed → পাসওয়ার্ড ঠিক? 2FA বন্ধ? বিশেষ চিহ্নযুক্ত পাসওয়ার্ড Env-এ ঠিকমতো বসেছে?
- [ ] MEGA quota/bandwidth → কিছু ঘণ্টা অপেক্ষা করুন বা `MEGA_LOGIN_FOR_DOWNLOAD=true` দিন।
- [ ] TeraBox "কুকি মেয়াদোত্তীর্ণ" → নতুন `ndus` নিন। অন্য errno আসলে লগ দেখুন; TeraBox এন্ডপয়েন্ট বদলে থাকতে পারে।
- [ ] Render-এ বট ঘুমিয়ে যায় → `/health` মনিটর (UptimeRobot) চালু আছে? Health Check Path সেট?
- [ ] OOM / হঠাৎ রিস্টার্ট → `WORKERS=1` রাখুন, বড় ফাইল একটা একটা করে পাঠান।
- [ ] `start.sh: not found` / `\r` এরর → ফাইলে CRLF ঢুকেছে; LF দিয়ে আবার সেভ করুন।
- [ ] Docker build ব্যর্থ → লগে কোন ধাপ (apt/rclone/pip) ব্যর্থ দেখুন; `requirements.txt`-এর ভার্সন রেঞ্জ সমস্যা করলে একটি নির্দিষ্ট ভার্সনে পিন করুন।
- [ ] Telegram থেকে ডাউনলোড ব্যর্থ → মূল মেসেজ ডিলিট হয়ে গেলে বট ফাইল আনতে পারে না; আবার ফরোয়ার্ড করুন।
