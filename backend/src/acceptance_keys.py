"""Deterministic RSA key pairs and helpers for acceptance and integration tests."""

from __future__ import annotations

import time
import uuid

import jwt

TEST_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAkT/53NcCX/znuaRvRzku
LWYJZpNPf0XEMlfvoN5qTzgU+0WVpu+n1WXNGmG19JA2XsRFHRH2VwHYf/CBdv/n
BuTsl/s2y9YBv0WdWaqtfXxRCoOeesN58ibuEIuNLdAhdbED8fRVkmlT56gKCF4L
jS2XUtlBhk+GfBYDhGLaofwqUz7mCkfK1mhYQOVXEccdzEnPnuJdJrIvM6rEtggi
4zTKFWLf4LwSUMeLofh5H2zoXPuxKCbpfMReXM/e1IYLJbYKQJtsW8mJQ4LRmXza
s0OBpvmPD5peYoqgB4C5DVD1KLcnFb/MLUwLlx1BoSd39ITGsgScEMpBvmFTD296
awIDAQAB
-----END PUBLIC KEY-----"""

TEST_PRIVATE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCRP/nc1wJf/Oe5
pG9HOS4tZglmk09/RcQyV++g3mpPOBT7RZWm76fVZc0aYbX0kDZexEUdEfZXAdh/
8IF2/+cG5OyX+zbL1gG/RZ1Zqq19fFEKg556w3nyJu4Qi40t0CF1sQPx9FWSaVPn
qAoIXguNLZdS2UGGT4Z8FgOEYtqh/CpTPuYKR8rWaFhA5VcRxx3MSc+e4l0msi8z
qsS2CCLjNMoVYt/gvBJQx4uh+HkfbOhc+7EoJul8xF5cz97UhgsltgpAm2xbyYlD
gtGZfNqzQ4Gm+Y8Pml5iiqAHgLkNUPUotycVv8wtTAuXHUGhJ3f0hMayBJwQykG+
YVMPb3prAgMBAAECggEAFeGucZ+Ogwllm2jzPqaVl09E4UgV9NV8zhzDQ9fd6htH
xebguapm/VX9xRitQa+sETC+PLflBYyIohWIW3YHxtkb73Vv9ttBniANqWF2C258
Avp7GIF0UI+e061CUoGFWOaW8o9diNYHRuHgyQvp75XLszZ5kZNPbkgetYXy6+4I
Cn2R/VwM2SMX3LvtRZtvyIZEy04q8kWVytu3xdgUNQnQCQw1v2hh75jOAApM3D9U
Ym4ZIrr9b4gZ7AgaxpcG8xaS31IlyfO6kH1dXdgJ06NjCwKSdYVCst2XnRUra14G
3Vus2BLjaHAH/QUcardjuRJH4D4a+kWnR6Uj7J/+wQKBgQDIGNlXyz5Sh5g1LOjl
8oYAZ9umB7O8LPpyRkc3BEVsh/GwrEnVnh47mwWlDPHzpBrgs6dp2MODTGX5mBjw
MrSymiCP+VL0R9MsIrk1e+1rvW9tZ7AGiTfiw+wcV0k4B1c+DVbV4bG1YXt7SfdJ
1dQz9E6gKnzR1d1lMbSfXIKpywKBgQC51Gah2cGQBz/rE0P7DVhCcj/Ri7JKkVAE
5UKA0aGtVH55tzDFTFYv6EZBGvftANNSrl93INDk8ZsbuN08F0G1oOJb6wALsN+B
tWqH4pzLqnjrgIJo93GYD49IDzOTXgu0RUo4Xpypavu5lFj8SNP+Q9XFzK8yYN6P
Zu/EK4zd4QKBgDG5axlUGnKr23krZdzuvboQFRdXRqT2kG4hZMUZPJ9R0Q/UYKzD
FEQ/q9FuPBoNp9g7DNlS0pMoQxcZYukd0KWpg3oDvUeTKbkID/poh4GEm9IWODoH
k2m4wk3wqvNPb7Ct4+xWn7CxPtxObzNb/dXDWl6W8Fm8yb3B8l6/wICtAoGAWDwg
F02b+NVYZjgZmUT22WYCI28fUwS4Z/qrnxrql7B89TIkeCoM6mZxpUw22FQCA0pE
jf4I8gw+R+V5NqUcUC96C4cD2Ik9Nwt679aL+LCTBc9ZHJoRV9Bbii2doaWQDSKx
YR6baF4h9DYaIoA6GIsBIBBWR6dzRcfspMrA8kECgYEAufjsNLrRHfgbrvUMF9TF
Wgig2uwagACf9rSX+R04oXuJBzcOasVE1Hd6h2Ih1Z2LNLJkLavSzoxOfo10aQza
vkbajtq+hixZE/S4Obo8Y7J551bNKF6OtjZScu3J31TXn4uqqYp6vKKFQrdXeZoY
xngKSkdDnuegVgbS0x9xzL8=
-----END PRIVATE KEY-----"""

ATTACKER_PRIVATE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCG5Lu42mwO6u6F
V4Oskg75xe2ITdBRh6k9E2h/nY7uv2wAM054CaLBjAD8t7fT/675RaZogS37NyGE
j34L7HzHSqXhL2P15MXdL4qvXfSNMNCfOvPqYAIttMVpCOQzhoAeMLfCJffEFYsF
ShFpo2yjQ9hKOFj4uJorEXLDkxcSBXgWKR+qTbJoYbOiHktl4Ln7+JADAzgRMvVu
UaA/H7Bwf8Q97xv/DrU7xEsocUySat+7x0jbiMRkqYrSDn4QGDzUfCqXfOUidi4/
XWE19B1U7tiUm4UNS86qmwF114zqpQf5Q0r7D0FBTGWTJcXg/fJIubCSkbp5ew6H
YunTa45dAgMBAAECggEAC1n2PiELVNoKRmhN6VjFL9mrGnxJ2oKLCFu4wHx2ybqM
ERxALzPIYXcxohKnwoxSoiFtVoAAaZKDHI+isMgu2+p5ORDu3YNp7yoQBp/mc3c3
Jx8j5YY8yLHcpvEbmwZng6Lf49OkHPkfIMvR2UzqWHbeHajOO+f0+UjXmoOnPlne
MWflXj89PSx0vGbHA7osqVrH+gJc/sewIpNSLWBOJFnbN+XBrUShUFA2dwgmtqbQ
2o8N9w1ASNwgoMteM0KUszjdjy2hgkETMH1f8PI7hfQBU5nk2scI/MD+t5Sax9ai
2ZtGS4qoZxdNbXUngi8cGBLAeYzNA71pMI0tjlYONQKBgQC6u4mQDOn1paf55IO1
uRVdFN8KSgGcFG0neV2KewtzTwTOYfdmzwUxPWurmy3joy1+dVzxY+27XSB8Lb6v
Pdky10JKvBwLxkXfkygvESWIZgptQzC251hRx2OMPaRvr1T/lkHO+knvwSuFDOEZ
BLzEaoo8+nag7qJZjG4ZHnhVdwKBgQC47naVaRgBcP78lyUsDHRy4XZkIApV+ymn
yc8lp728hSRFG5AHHaQcHFrQsrHaKMZKyns2vKq1kCxh68AkS7Ap41gyM7RfAV3G
9tK4snggyNnGvCubqOdiKPwova90G3MvsTc98xRTalNDjAsp8Yg7yPql2g3j/w9A
nCblbqS/ywKBgAs3subdb7ybDAE+Eg97o+XomBGFKp2WuIvzhYQJKkHuEErZpcQe
UaCIoQw5EUOojNYY9NFngGOOD6KGGPtsCr08IzZcII8whQgOTg6tyARupNe1fpqu
QScAhGFrXpJ7BO8ptqBZdtGZWCTxBiC7oR9nixBot3GphEU+42pM40u3AoGAS6tH
WltmNzDFJRWzYBK4k0kt6Eoz8HqU0NMCr6WzPTA9ZwmgJUk7nUT84+g6XcnRWUkA
oaURNLmJb4fHVxKhQoKD1AbUBwvIE2FrV6eu/7svYOId9dNm2gNrKkBzSk7xEtKw
h+iuqHMZHspuxvrNg6j45mQSyvfShd1fDQH454ECgYAX24/LhOUWKM48KvccQd2z
wMZtPDfJ8C/InsmJh6KCr4wm/PHdMm2jXphNkUMUvfV+yhk4uaqudovbrPXnREcZ
jCVyIffPL2QHbS2nysNMB4KAHB46vt/WTwxxvToonJjVrjdhxyFPrP1xq24eT+/D
nHgZj5xosx4hbPtwQ++4wQ==
-----END PRIVATE KEY-----"""


def generate_jwt(
    *,
    sub: str | uuid.UUID,
    email: str = "traveler@swena.internal",
    name: str = "Karnataka Explorer",
    issuer: str = "https://oauth21.vercel.app",
    audience: str = "swena_travel_agent_client",
    expires_in_seconds: int = 3600,
    is_forged: bool = False,
) -> str:
    """Generates an RS256 JWT using trusted or attacker private key."""
    signing_key = ATTACKER_PRIVATE_KEY_PEM if is_forged else TEST_PRIVATE_KEY_PEM
    payload = {
        "sub": str(sub),
        "email": email,
        "name": name,
        "iss": issuer,
        "aud": audience,
        "exp": int(time.time()) + expires_in_seconds,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, signing_key, algorithm="RS256")
