"""Fixture comum: banco em memória, semeado de forma determinística."""
from __future__ import annotations

import random
import unittest
from datetime import date, datetime

from clinica import db, tools
from clinica.seed import semear

DATA_BASE = date(2026, 1, 5)            # segunda-feira
AGORA = datetime(2026, 1, 5, 7, 0)      # antes de qualquer slot semeado


class BaseClinica(unittest.TestCase):
    def setUp(self):
        self.conn = db.conectar(":memory:")
        db.criar_schema(self.conn)
        semear(self.conn, DATA_BASE, random.Random(7))
        self.addCleanup(self.conn.close)

    def paciente(self):
        return self.conn.execute("SELECT * FROM pacientes LIMIT 1").fetchone()

    def slot_livre(self, especialidade="Ortopedia", **filtros):
        r = tools.consultar_agenda(self.conn, especialidade=especialidade,
                                   agora=AGORA, limite=1, **filtros)
        self.assertTrue(r["slots"], f"fixture sem slot livre para {especialidade}")
        return r["slots"][0]

    def status_slot(self, slot_id):
        return self.conn.execute("SELECT status FROM slots WHERE id=?", (slot_id,)).fetchone()[0]
