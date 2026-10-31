# ai-review

Pull request açıldığında GLM Coding Plan (Pi + `zai/glm-5.3`) ile otomatik kod incelemesi.
İnceleme s10'daki self-hosted runner'da (`s10-review`) çalışır; GLM istekleri ev IP'sinden çıkar.

## Ne yapar
- PR başlığı, açıklaması, önceki yorumları ve diff'i okur.
- Projenin **ana dalındaki** `CLAUDE.md` / `AGENTS.md` (varsa `.github/ai-review.md`) dosyasını proje kuralı olarak kullanır.
- Doğruluk, güvenlik, kod kalitesi, kapsam dışı değişiklikler ve açıklama–kod tutarlılığına bakar.
- Sonucu satır bazlı yorumlar + özet olarak tek bir PR review'u halinde yazar (`github-actions[bot]`).

## Dosyalar
| Dosya | Görev |
|---|---|
| `review.py` | PR verisini `gh` ile toplar, Pi'yi çalıştırır, review'u gönderir |
| `system.md` | Modelin inceleme talimatı ve JSON çıktı biçimi |
| `run.sh` | Runner'da GLM anahtarını yükleyip `review.py`'yi çağırır |
| `.github/workflows/review.yml` | Projelerin çağırdığı ortak (reusable) workflow |
| `caller-example.yml` | Projeye kopyalanacak workflow |

## Yeni projeye eklemek
1. s10'da o repo için bir `s10-review` runner'ı kaydet (`/opt/gha-review/runners/<repo>`).
2. `caller-example.yml` dosyasını projede `.github/workflows/ai-review.yml` olarak ekle.
3. Projede güncel bir `CLAUDE.md` olsun.

## s10 tarafı
- Kullanıcı `gha-review` (sistem kullanıcısı, docker/sudo yok), home `/opt/gha-review`.
- GLM anahtarı `/opt/gha-review/.config/glm/zai.env` (600). GitHub'a hiç gitmez.
- Bu repo `/opt/gha-review/ai-review` altında, salt-okunur deploy key ile her işte güncellenir.
