/**
 * auth.js — Xác thực đơn giản (username/password cố định)
 *
 * Luồng:
 *   1. login.html gọi login() khi submit form
 *   2. Nếu đúng → lưu sessionStorage → redirect index.html
 *   3. index.html gọi requireAuth() khi load → nếu chưa login → redirect login.html
 *   4. Nút đăng xuất gọi logout() → xóa session → redirect login.html
 *
 * ⚠️ Đây là xác thực phía client, chỉ dùng để demo/bảo vệ cơ bản.
 *    KHÔNG dùng cho môi trường production thực sự.
 */
'use strict';

// ═══════════════════════════════════════════════════════════════
// CẤU HÌNH TÀI KHOẢN
// ═══════════════════════════════════════════════════════════════
const AUTH_USERS = [
  { username: 'admin',  password: 'cps2026' },
  { username: 'nhom5',  password: 'hcmute'  },
];

const SESSION_KEY = 'cps_auth';

// ═══════════════════════════════════════════════════════════════
// KIỂM TRA SESSION — gọi ở đầu index.html
// ═══════════════════════════════════════════════════════════════
function requireAuth() {
  if (!sessionStorage.getItem(SESSION_KEY)) {
    window.location.href = 'login.html';
  }
}

// ═══════════════════════════════════════════════════════════════
// ĐĂNG NHẬP — gọi từ login.html
// ═══════════════════════════════════════════════════════════════
function login(username, password) {
  const user = AUTH_USERS.find(
    u => u.username === username.trim() && u.password === password
  );

  if (user) {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ username: user.username }));
    window.location.href = 'index.html';
    return true;
  }
  return false;
}

// ═══════════════════════════════════════════════════════════════
// ĐĂNG XUẤT — gọi từ nút logout trong index.html
// ═══════════════════════════════════════════════════════════════
function logout() {
  sessionStorage.removeItem(SESSION_KEY);
  window.location.href = 'login.html';
}

// ═══════════════════════════════════════════════════════════════
// LẤY TÊN USER ĐANG ĐĂNG NHẬP
// ═══════════════════════════════════════════════════════════════
function getAuthUser() {
  const raw = sessionStorage.getItem(SESSION_KEY);
  return raw ? JSON.parse(raw) : null;
}