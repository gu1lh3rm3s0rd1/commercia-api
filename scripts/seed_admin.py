"""One-off script to seed the first company/group/permission/user for local testing.

Run manually, from the project root, AFTER the Fase 0 schema has been applied to the
database yourself (this project never runs migrations automatically — see the
commercia-never-run-migrations memory):

    venv/Scripts/python.exe -m scripts.seed_admin

Idempotent: safe to run more than once, it only creates rows that don't already exist.
"""

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.company import Company
from app.models.user import Group, GroupPermission, Permission, User, UserCompany, UserGroup

DEMO_FILIAL_CODE = "MATRIZ"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"  # dev-only seed password — change/remove before any real use.


def seed() -> None:
    db = SessionLocal()
    try:
        company = db.execute(
            select(Company).where(Company.filial_code == DEMO_FILIAL_CODE)
        ).scalar_one_or_none()
        if company is None:
            company = Company(
                filial_code=DEMO_FILIAL_CODE,
                corporate_name="Commercia Demo LTDA",
                cnpj="00000000000100",
            )
            db.add(company)
            db.flush()

        admin_group = db.execute(
            select(Group).where(Group.name == "Administrador")
        ).scalar_one_or_none()
        if admin_group is None:
            admin_group = Group(name="Administrador")
            db.add(admin_group)
            db.flush()

        for code, name in (
            ("users.manage", "Gerenciar usuários"),
            ("products.edit", "Gerenciar catálogo (categorias/produtos/preços/estoque)"),
        ):
            perm = db.execute(select(Permission).where(Permission.code == code)).scalar_one_or_none()
            if perm is None:
                perm = Permission(name=name, code=code)
                db.add(perm)
                db.flush()
            already_granted = db.execute(
                select(GroupPermission).where(
                    GroupPermission.group_id == admin_group.id,
                    GroupPermission.permission_id == perm.id,
                )
            ).scalar_one_or_none()
            if already_granted is None:
                db.add(GroupPermission(group_id=admin_group.id, permission_id=perm.id))

        admin_user = db.execute(
            select(User).where(User.username == ADMIN_USERNAME)
        ).scalar_one_or_none()
        if admin_user is None:
            admin_user = User(
                username=ADMIN_USERNAME,
                email="admin@commercia.local",
                password_hash=hash_password(ADMIN_PASSWORD),
                is_superuser=True,
            )
            db.add(admin_user)
            db.flush()
            db.add(UserGroup(user_id=admin_user.id, group_id=admin_group.id))
            db.add(UserCompany(user_id=admin_user.id, filial_id=company.id))

        db.commit()
        print(
            f"Seed ok — login with username '{ADMIN_USERNAME}' / password '{ADMIN_PASSWORD}', "
            f"filial '{company.filial_code}' (id={company.id})"
        )
    finally:
        db.close()


if __name__ == "__main__":
    seed()
