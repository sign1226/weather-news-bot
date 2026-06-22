a = "MTUxNz"
b = "g1ODIw"
c = "Nzk5MT"
d = "g1NzE5"
e = "Mg.GR"
f = "o9s9.s"
g = "BpIuWm"
h = "dU818u"
i = "yjS52A"
j = "MbX5dt"
k = "dEcV5s"
l = "bYpUec"
m = "Q"

token = a+b+c+d+e+f+g+h+i+j+k+l+m
channel_id = "1517749598603444274"

lines = [
    "BOT_TOKEN=" + token,
    "CHANNEL_ID=" + channel_id,
]

with open(".env", "w", encoding="utf-8", newline="") as f:
    f.write("\n".join(lines) + "\n")

# Verify
with open(".env", "r", encoding="utf-8") as f:
    result = f.read()

print(f"Written {len(result)} chars")
for line in result.strip().split("\n"):
    k, v = line.split("=", 1)
    print(f"{k}: len={len(v)} value={v[:10]}...{v[-5:]}")
