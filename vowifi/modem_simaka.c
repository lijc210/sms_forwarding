/*
 * modem-simaka: strongSwan simaka_card_t 后端插件
 *
 * 把 EAP-AKA 挑战（RAND/AUTN）转发给外部助手进程（串口 AT 指令访问
 * ML307A 等蜂窝模组上的 USIM），取回 RES/CK/IK 或 AUTS。
 *
 * 参考 strongSwan eap_sim_pcsc 插件结构与 simaka_card_t 接口。
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define _GNU_SOURCE

#include <daemon.h>
#include <simaka_manager.h>

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#define DEFAULT_HELPER "/usr/local/libexec/simaka-helper"
#define HELPER_OUT_MAX 512

/** 卡对象：公开 simaka_card_t 接口 + 销毁方法 */
typedef struct modem_simaka_card_t {
	simaka_card_t card;
	void (*destroy)(struct modem_simaka_card_t *this);
} modem_simaka_card_t;

typedef struct private_modem_simaka_card_t {
	modem_simaka_card_t public;

	/** 助手程序路径（strongswan.conf: charon.plugins.modem-simaka.helper） */
	char *helper;

	/** 上次挑战返回的 AUTS（同步失败重同步用） */
	char auts[AKA_AUTS_LEN];
	bool have_auts;
} private_modem_simaka_card_t;

typedef struct private_modem_simaka_plugin_t {
	plugin_t public;
	modem_simaka_card_t *card;
} private_modem_simaka_plugin_t;

/**
 * 十六进制编码 len 字节 → 大写 hex 字符串（dst 至少 len*2+1）
 */
static void to_hex(const char *src, size_t len, char *dst)
{
	static const char digits[] = "0123456789ABCDEF";
	for (size_t i = 0; i < len; i++)
	{
		dst[i * 2] = digits[(src[i] >> 4) & 0xF];
		dst[i * 2 + 1] = digits[src[i] & 0xF];
	}
	dst[len * 2] = '\0';
}

/**
 * 解析十六进制字符串 → 字节。返回字节数，非法输入返回 -1。
 */
static int from_hex(const char *hex, char *dst, size_t dst_max)
{
	size_t len = strlen(hex);
	if (len == 0 || len % 2 != 0 || len / 2 > dst_max)
	{
		return -1;
	}
	for (size_t i = 0; i < len / 2; i++)
	{
		unsigned int byte;
		if (sscanf(hex + i * 2, "%2x", &byte) != 1)
		{
			return -1;
		}
		dst[i] = (char)byte;
	}
	return (int)(len / 2);
}

/**
 * 执行助手: <helper> aka <RAND_hex> <AUTN_hex>，读回一行输出。
 * 输出经管道返回（避免密钥出现在命令行）。返回 TRUE 且填充 out。
 */
static bool run_helper(private_modem_simaka_card_t *this,
					   const char rand_hex[AKA_RAND_LEN * 2 + 1],
					   const char autn_hex[AKA_AUTN_LEN * 2 + 1],
					   char *out, size_t out_len)
{
	int fds[2];
	if (pipe(fds) != 0)
	{
		return FALSE;
	}
	pid_t pid = fork();
	if (pid < 0)
	{
		close(fds[0]);
		close(fds[1]);
		return FALSE;
	}
	if (pid == 0)
	{
		/* 子进程：stdout 接管道，stderr 丢弃 */
		dup2(fds[1], STDOUT_FILENO);
		close(fds[0]);
		close(fds[1]);
		int devnull = open("/dev/null", O_WRONLY);
		if (devnull >= 0)
		{
			dup2(devnull, STDERR_FILENO);
			close(devnull);
		}
		char *const argv[] = { this->helper, (char*)"aka",
							   (char*)rand_hex, (char*)autn_hex, NULL };
		execv(this->helper, argv);
		_exit(127);
	}
	close(fds[1]);

	size_t total = 0;
	ssize_t n;
	while (total < out_len - 1 &&
		   (n = read(fds[0], out + total, out_len - 1 - total)) > 0)
	{
		total += (size_t)n;
	}
	out[total] = '\0';
	close(fds[0]);

	int status = 0;
	waitpid(pid, &status, 0);
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
	{
		return FALSE;
	}
	return total > 0;
}

METHOD(simaka_card_t, get_triplet, bool,
	private_modem_simaka_card_t *this, identification_t *id,
	char rand[SIM_RAND_LEN], char sres[SIM_SRES_LEN], char kc[SIM_KC_LEN])
{
	(void)this; (void)id; (void)rand; (void)sres; (void)kc;
	/* 不支持 2G 三元组（EAP-SIM） */
	return FALSE;
}

METHOD(simaka_card_t, get_quintuplet, status_t,
	private_modem_simaka_card_t *this, identification_t *id,
	char rand[AKA_RAND_LEN], char autn[AKA_AUTN_LEN],
	char ck[AKA_CK_LEN], char ik[AKA_IK_LEN],
	char res[AKA_RES_MAX], int *res_len)
{
	char rand_hex[AKA_RAND_LEN * 2 + 1], autn_hex[AKA_AUTN_LEN * 2 + 1];
	char out[HELPER_OUT_MAX];

	(void)id;
	to_hex(rand, AKA_RAND_LEN, rand_hex);
	to_hex(autn, AKA_AUTN_LEN, autn_hex);

	if (!run_helper(this, rand_hex, autn_hex, out, sizeof(out)))
	{
		DBG1(DBG_CFG, "modem-simaka: helper failed: %s", out[0] ? out : "(no output)");
		return FAILED;
	}

	status_t status = FAILED;
	if (strncmp(out, "OK ", 3) == 0)
	{
		/* OK <RES_hex> <CK_hex> <IK_hex> */
		char *saveptr = NULL;
		char *res_h = strtok_r(out + 3, " \t\r\n", &saveptr);
		char *ck_h = strtok_r(NULL, " \t\r\n", &saveptr);
		char *ik_h = strtok_r(NULL, " \t\r\n", &saveptr);
		char res_b[AKA_RES_MAX], ck_b[AKA_CK_LEN], ik_b[AKA_IK_LEN];
		int res_l, ck_l, ik_l;
		if (res_h && ck_h && ik_h &&
			(res_l = from_hex(res_h, res_b, sizeof(res_b))) >= 4 &&
			(ck_l = from_hex(ck_h, ck_b, sizeof(ck_b))) == AKA_CK_LEN &&
			(ik_l = from_hex(ik_h, ik_b, sizeof(ik_b))) == AKA_IK_LEN)
		{
			memcpy(res, res_b, (size_t)res_l);
			memcpy(ck, ck_b, AKA_CK_LEN);
			memcpy(ik, ik_b, AKA_IK_LEN);
			*res_len = res_l;
			status = SUCCESS;
			DBG1(DBG_CFG, "modem-simaka: AKA quintuplet obtained (RES %d bytes)", res_l);
		}
		else
		{
			DBG1(DBG_CFG, "modem-simaka: malformed helper response");
		}
		memset(res_b, 0, sizeof(res_b));
		memset(ck_b, 0, sizeof(ck_b));
		memset(ik_b, 0, sizeof(ik_b));
	}
	else if (strncmp(out, "AUTS ", 5) == 0)
	{
		/* AUTS <hex> —— 同步失败，保存 AUTS 供 resync 使用 */
		char auts_b[AKA_AUTS_LEN];
		int auts_l = from_hex(out + 5, auts_b, sizeof(auts_b));
		if (auts_l == AKA_AUTS_LEN)
		{
			memcpy(this->auts, auts_b, AKA_AUTS_LEN);
			this->have_auts = TRUE;
			status = INVALID_STATE;
			DBG1(DBG_CFG, "modem-simaka: sync failure, AUTS stored");
		}
	}
	else
	{
		DBG1(DBG_CFG, "modem-simaka: helper rejected challenge: %s", out);
	}
	memset(out, 0, sizeof(out));
	return status;
}

METHOD(simaka_card_t, resync, bool,
	private_modem_simaka_card_t *this, identification_t *id,
	char rand[AKA_RAND_LEN], char auts[AKA_AUTS_LEN])
{
	(void)id; (void)rand;
	if (this->have_auts)
	{
		memcpy(auts, this->auts, AKA_AUTS_LEN);
		this->have_auts = FALSE;
		memset(this->auts, 0, sizeof(this->auts));
		return TRUE;
	}
	return FALSE;
}

METHOD(simaka_card_t, set_pseudonym, void,
	private_modem_simaka_card_t *this, identification_t *id,
	identification_t *pseudonym)
{
	(void)this; (void)id; (void)pseudonym;
}

METHOD(simaka_card_t, get_pseudonym, identification_t*,
	private_modem_simaka_card_t *this, identification_t *id)
{
	(void)this; (void)id;
	return NULL;
}

METHOD(simaka_card_t, set_reauth, void,
	private_modem_simaka_card_t *this, identification_t *id,
	identification_t *next, char mk[HASH_SIZE_SHA1], uint16_t counter)
{
	(void)this; (void)id; (void)next; (void)mk; (void)counter;
}

METHOD(simaka_card_t, get_reauth, identification_t*,
	private_modem_simaka_card_t *this, identification_t *id,
	char mk[HASH_SIZE_SHA1], uint16_t *counter)
{
	(void)this; (void)id; (void)mk; (void)counter;
	return NULL;
}

METHOD(modem_simaka_card_t, card_destroy, void,
	private_modem_simaka_card_t *this)
{
	memset(this->auts, 0, sizeof(this->auts));
	free(this->helper);
	free(this);
}

static modem_simaka_card_t *card_create(const char *helper)
{
	private_modem_simaka_card_t *this;

	INIT(this,
		.public = {
			.card = {
				.get_triplet = _get_triplet,
				.get_quintuplet = _get_quintuplet,
				.resync = _resync,
				.set_pseudonym = _set_pseudonym,
				.get_pseudonym = _get_pseudonym,
				.set_reauth = _set_reauth,
				.get_reauth = _get_reauth,
			},
			.destroy = _card_destroy,
		},
		.helper = strdup(helper),
	);
	return &this->public;
}

METHOD(plugin_t, get_name, char*,	private_modem_simaka_plugin_t *this)
{
	(void)this;
	return "modem-simaka";
}

/**
 * Callback providing our card to register
 */
static simaka_card_t* get_card(private_modem_simaka_plugin_t *this)
{
	return &this->card->card;
}

METHOD(plugin_t, get_features, int,
	private_modem_simaka_plugin_t *this, plugin_feature_t *features[])
{
	(void)this;
	static plugin_feature_t f[] = {
		PLUGIN_CALLBACK(simaka_manager_register, get_card),
			PLUGIN_PROVIDE(CUSTOM, "aka-card"),
				PLUGIN_DEPENDS(CUSTOM, "aka-manager"),
	};
	*features = f;
	return countof(f);
}

METHOD(plugin_t, destroy, void,
	private_modem_simaka_plugin_t *this)
{
	this->card->destroy(this->card);
	free(this);
}

/*
 * See header
 */
PLUGIN_DEFINE(modem_simaka)
{
	private_modem_simaka_plugin_t *this;
	const char *helper = lib->settings->get_str(lib->settings,
						"%s.plugins.modem-simaka.helper", DEFAULT_HELPER, lib->ns);

	INIT(this,
		.public = {
			.get_name = _get_name,
			.get_features = _get_features,
			.destroy = _destroy,
		},
		.card = card_create(helper),
	);

	return &this->public;
}
