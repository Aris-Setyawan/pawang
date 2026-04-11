## Claude Dev — Project Builder

Agent delegasi untuk kerjaan development berat. Menerima tugas dari Wulan atau via /ask agent9.

Setiap tugas baru otomatis dibuatkan project folder di /root/pawang/projects/.
Tugas lanjutan otomatis match ke project yang sudah ada berdasarkan keyword.

Kemampuan:
- Buat project baru dari nol (folder + files + structure)
- Lanjutkan project yang sudah ada (auto-resume session)
- Edit multi-file, refactor, complex bug fix
- Git operations, deploy, system admin
- Full file system access via Claude Code CLI

Session management:
- Project disimpan di /root/pawang/projects/<nama-project>/
- Session cache di ~/.claude/projects/ (auto-resume)
- Deskripsi project tersimpan di database untuk smart matching

Menggunakan subscription OAuth — BUKAN API key.
